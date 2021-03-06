import logging

from django.apps import apps
from django_elasticsearch_dsl.registries import registry

from readthedocs.worker import app

log = logging.getLogger(__name__)


def _get_index(indices, index_name):
    """
    Get Index from all the indices

    :param indices: DED indices list
    :param index_name: Name of the index
    :return: DED Index
    """
    for index in indices:
        if str(index) == index_name:
            return index


def _get_document(model, document_class):
    """
    Get DED document class object from the model and name of document class

    :param model: The model class to find the document
    :param document_class: the name of the document class.
    :return: DED DocType object
    """
    documents = registry.get_documents(models=[model])

    for document in documents:
        if str(document) == document_class:
            return document


@app.task(queue='web')
def create_new_es_index(app_label, model_name, index_name, new_index_name):
    model = apps.get_model(app_label, model_name)
    indices = registry.get_indices(models=[model])
    old_index = _get_index(indices=indices, index_name=index_name)
    new_index = old_index.clone(name=new_index_name)
    new_index.create()


@app.task(queue='web')
def switch_es_index(app_label, model_name, index_name, new_index_name):
    model = apps.get_model(app_label, model_name)
    indices = registry.get_indices(models=[model])
    old_index = _get_index(indices=indices, index_name=index_name)
    new_index = old_index.clone(name=new_index_name)
    old_index_actual_name = None

    if old_index.exists():
        # Alias can not be used to delete an index.
        # https://www.elastic.co/guide/en/elasticsearch/reference/6.0/indices-delete-index.html
        # So get the index actual name to delete it
        old_index_info = old_index.get()
        # The info is a dictionary and the key is the actual name of the index
        old_index_actual_name = list(old_index_info.keys())[0]

    # Put alias into the new index name and delete the old index if its exist
    new_index.put_alias(name=index_name)
    if old_index_actual_name:
        old_index.connection.indices.delete(index=old_index_actual_name)


@app.task(queue='web')
def index_objects_to_es(app_label, model_name, document_class, index_name,
                        chunk=None, objects_id=None):

    assert not (chunk and objects_id), "You can not pass both chunk and objects_id"

    model = apps.get_model(app_label, model_name)
    document = _get_document(model=model, document_class=document_class)
    doc_obj = document()

    # WARNING: This must use the exact same queryset as from where we get the ID's
    # There is a chance there is a race condition here as the ID's may change as the task runs,
    # so we need to think through this a bit more and probably pass explicit ID's,
    # but there are performance issues with that on large model sets
    queryset = doc_obj.get_queryset()
    if chunk:
        # Chunk is a tuple with start and end index of queryset
        start = chunk[0]
        end = chunk[1]
        queryset = queryset[start:end]
    elif objects_id:
        queryset = queryset.filter(id__in=objects_id)

    log.info("Indexing model: {}, '{}' objects".format(model.__name__, queryset.count()))
    doc_obj.update(queryset.iterator(), index_name=index_name)


@app.task(queue='web')
def index_missing_objects(app_label, model_name, document_class, index_generation_time):
    """
    Task to insure that none of the object is missed from indexing.

    The object ids are sent to `index_objects_to_es` task for indexing.
    While the task is running, new objects can be created/deleted in database
    and they will not be in the tasks for indexing into ES.
    This task will index all the objects that got into DB after the `latest_indexed` timestamp
    to ensure that everything is in ES index.
    """
    model = apps.get_model(app_label, model_name)
    document = _get_document(model=model, document_class=document_class)
    queryset = document().get_queryset().exclude(modified_date__lte=index_generation_time)
    document().update(queryset.iterator())

    log.info("Indexed {} missing objects from model: {}'".format(queryset.count(), model.__name__))

    # TODO: Figure out how to remove the objects from ES index that has been deleted
