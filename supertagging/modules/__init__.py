"""
Django-SuperTagging
"""
import re, datetime
from django.contrib.contenttypes.models import ContentType
from django.template.defaultfilters import slugify
from django.utils.encoding import force_unicode
from django.db.models.loading import get_model

from supertagging import settings
from supertagging.calais import Calais
from supertagging.models import SuperTag, SuperTagRelation, SuperTaggedItem, SuperTaggedRelationItem, SuperTagProcessQueue, SuperTagExclude

REF_REGEX = "^http://d.opencalais.com/(?P<key>.*)$"

def add_to_queue(instance):
    cont_type = ContentType.objects.get_for_model(instance)
    SuperTagProcessQueue.objects.get_or_create(content_type=cont_type, object_id=instance.pk)
    
def remove_from_queue(instance):
    cont_type = ContentType.objects.get_for_model(instance)
    try:
        SuperTagProcessQueue.objects.get(content_type=cont_type, object_id=instance.pk).delete()
    except SuperTagProcessQueue.DoesNotExist:
        pass

def process(obj, tags=[]):
    """
    Process the data.
    """
    # In the case when we want to turn off ALL processing of data, while
    # preserving AUTO_PROCESS 
    if not settings.ENABLED:
        return

    if not settings.API_KEY:
        if settings.ST_DEBUG:
            raise ValueError('Calais API KEY is missing.')
        return

    try:
        params = settings.MODULES['%s.%s' % (obj._meta.app_label, 
            obj._meta.module_name)]
        model = get_model(obj._meta.app_label, obj._meta.module_name)
    except KeyError, e:
        if settings.ST_DEBUG:
            raise KeyError(e)
        return
        
    if params.has_key('match_kwargs'):
        try:
            # Make sure this obj matches the match kwargs
            obj = model.objects.get(pk=obj.pk, **params['match_kwargs'])
        except model.DoesNotExist:
            return
        
    process_type = settings.DEFAULT_PROCESS_TYPE
    if 'contentType' in settings.PROCESSING_DIR:
        d_proc_type = proc_dir['contentType']

    if 'fields' not in params:
        if settings.ST_DEBUG:
            raise Exception('No "fields" found.')
        else:
            return

    # Create the instance of Calais and setup the parameters,
    # see open-calais.com for more information about user directives,
    # and processing directives
    c = Calais(settings.API_KEY)
    c.user_directives.update(settings.USER_DIR)
    c.processing_directives.update(settings.PROCESSING_DIR)
    c.processing_directives['contentType'] = process_type

    # Get the object's date.
    # First look for get_latest_by in the meta class, if nothing is
    # found check the ordering attribute in the meta class.
    date = None
    date_fields = []
    if obj._meta.get_latest_by:
        date_fields.append(obj._meta.get_latest_by)
    else:
        date_fields = obj._meta.ordering
    
    for f in date_fields:
        f=f.lstrip('-')
        date = getattr(obj, f, None)
        if isinstance(date, datetime.datetime) or isinstance(date, datetime.date):
            break
        date = None
        continue
        
    processed_tags = []
    for item in params['fields']:
        try:
            d = item.copy()
            
            field = d.pop('name')
            proc_type = d.pop('process_type', process_type)
            markup = d.pop('markup', False)

            data = getattr(obj, field)

            data = force_unicode(getattr(obj, field))
            
            # Analyze the text (data)
            result = c.analyze(data)

            # Retrieve the Django content type for the obj
            ctype = ContentType.objects.get_for_model(obj)
            # Remove existing items, this ensures tagged items 
            # are updated correctly
            SuperTaggedItem.objects.filter(content_type=ctype, 
                object_id=obj.pk, field=field).delete()
            if settings.PROCESS_RELATIONS:
                SuperTaggedRelationItem.objects.filter(content_type=ctype, 
                    object_id=obj.pk, field=field).delete()

            entities, relations, topics = [], [], []
            # Process entities, relations and topics
            if hasattr(result, 'entities'):
                entities = _processEntities(field, result.entities, 
                    obj, ctype, proc_type, tags, date)

            if hasattr(result, 'relations') and settings.PROCESS_RELATIONS:
                relations = _processRelations(field, result.relations, obj, 
                    ctype, proc_type, tags, date)                

            if hasattr(result, 'topics') and settings.PROCESS_TOPICS:
                topics =  _processTopics(field, result.topics, obj, 
                    ctype, tags, date)
                
            processed_tags.extend(entities)
            processed_tags.extend(topics)
        except Exception, e:
            if settings.ST_DEBUG: raise Exception(e)
            continue
    return processed_tags

def clean_up(obj):
    """
    When an object is removed, remove all the super tagged items 
    and super tagged relation items
    """
    try:
        cont_type = ContentType.objects.get_for_model(obj)
        SuperTaggedItem.objects.filter(content_type=cont_type, 
            object_id=obj.pk).delete()
        SuperTaggedRelationItem.objects.filter(content_type=cont_type, 
            object_id=obj.pk).delete()
    except Exception, e:
        if settings.ST_DEBUG: raise Exception(e)
    # TODO, clean up tags that have no related items?
    # Same for relations?

def _processEntities(field, data, obj, ctype, process_type, tags, date):
    """
    Process Entities.
    """
    processed_tags = []
    for e in data:
        entity = e.copy()
        # Here we convert the given float value to an integer
        rel = int(float(str(entity.pop('relevance', '0'))) * 1000)
        
        # Only process tags and items that greater or equal 
        # to MIN_RELEVANCE setting
        if rel < settings.MIN_RELEVANCE:
            continue
            
        inst = entity.pop('instances', {})
        ## Tidy up the encoding
        for i, j in enumerate(inst):
            for k, v in j.items():
                if isinstance(v, unicode):
                    inst[i][k] = v.encode('utf-8')
                else:
                    inst[i][k] = v


        pk = re.match(REF_REGEX, str(entity.pop('__reference'))).group('key')
        stype = entity.pop('_type', '')

        if stype.lower() not in settings.EXCLUSIONS:
            name = entity.pop('name', '').lower()
            if tags and name not in tags:
                continue

            slug = slugify(name)
            tag = None
            try:
                tag = SuperTag.objects.get_by_name(name__iexact=name)
            except SuperTag.DoesNotExist:
                try:
                    tag = SuperTag.objects.get(pk=pk)
                except SuperTag.DoesNotExist:
                    tag = SuperTag.objects.create_alternate(id=pk, slug=slug, 
                        stype=stype, name=name)
            except SuperTag.MultipleObjectsReturned:
                tag = SuperTag.objects.filter(name__iexact=name)[0]
                
            tag = tag.substitute or tag
            
            # If this tag was added to exlcude list, move onto the next item.
            if len(SuperTagExclude.objects.filter(tag__pk=tag.pk)) == 1:
                continue
                
            tag.properties = entity
            tag.save()
                
            # Check to make sure that the entity is not already attached
            # to the content object, if it is, just append the instances. This
            # should elimiate entities returned with different names such as
            # 'Washington' and 'Washington DC' but same id
            try:
                it = SuperTaggedItem.objects.get(tag=tag, content_type=ctype, 
                    object_id=obj.pk, field=field)
                it.instances.append(inst)
                it.item_date = date
                # Take the higher relevance
                if rel > it.relevance:
                    it.relevance = rel
                it.save()
            except SuperTaggedItem.DoesNotExist:
                # Create the record that will associate content to tags
                it = SuperTaggedItem.objects.create(tag=tag, 
                    content_type=ctype, object_id=obj.pk, field=field, 
                    process_type=process_type, relevance=rel, instances=inst, 
                    item_date=date)

            processed_tags.append(tag)
    return processed_tags

def _processRelations(field, data, obj, ctype, process_type, tags, date):
    """
    Process Relations
    """
    for d in data:
        di = d.copy()
        di.pop('__reference')
        inst = di.pop('instances', {})
        rel_type = di.pop('_type', '')

        props = {}
        entities = {}
        # Loop all the items in search of entities (SuperTags).
        for k,v in di.items():
            if isinstance(v, dict):
                ref = v.pop('__reference', '')
            else:
                ref = v
                
            res = re.match(REF_REGEX, unicode(ref))
            if res:
                entities[k] = res.group('key')
            else:
                props[k] = v

        # Make a copy of the found entities
        entities_temp = entities.copy()
        # This double loop builds a properties dict that includes entities as "Text" and not
        # as the "ID", since the SuperTagRelation needs one "entity" we take one and try to
        # resolve the other found entities. Entities should already exist from the previous
        # opertaion "_processEntities"
        for entity_key,entity_value in entities.items():
            _vals = {}
            for entity_temp_key,entity_temp_value in entities_temp.items():
                if entity_key != entity_temp_key and entity_value != entity_temp_value:
                    _vals[entity_temp_key] = _getEntityText(entity_temp_value)

            _vals.update(props)

            if tags and entity_value not in tags:
                continue
            
            try:
                entity = SuperTag.objects.get(pk=entity_value)
                entity = entity.substitute or entity
            except SuperTag.DoesNotExist:
                continue
                
            if len(SuperTagExclude.objects.filter(tag__pk=entity.pk)) == 1:
                continue
                
            rel_item, rel_created = SuperTagRelation.objects.get_or_create(
                tag=entity, name=entity_key, stype=rel_type, properties=_vals)

            SuperTaggedRelationItem.objects.create(relation=rel_item, 
                content_type=ctype, object_id=obj.pk, field=field, 
                process_type=process_type, instances=inst, item_date=date)

def _processTopics(field, data, obj, ctype, tags, date):
    """
    Process Topics, this opertaion is similar to _processEntities, the only
    difference is that there are no instances
    """
    processed_tags = []
    for di in data:
        di.pop('__reference')

        pk = re.match(REF_REGEX, str(di.pop('category'))).group('key')
        stype = 'Topic'
        name = di.pop('categoryName', '').lower()
        if tags and name not in tags:
            continue

        slug = slugify(name)
        tag = None
        try:
            tag = SuperTag.objects.get_by_name(name__iexact=name)
        except SuperTag.DoesNotExist:
            try:
                tag = SuperTag.objects.get(pk=pk)
            except SuperTag.DoesNotExist:
                tag = SuperTag.objects.create_alternate(id=pk, slug=slug, 
                    stype=stype, name=name)
        except SuperTag.MultipleObjectsReturned:
            tag = SuperTag.objects.filter(name__iexact=name)[0]
            
        tag = tag.substitute or tag
        
        if len(SuperTagExclude.objects.filter(tag__pk=tag.pk)) == 1:
            continue

        tag.properties = di
        tag.save()

        SuperTaggedItem.objects.create(tag=tag, content_type=ctype, 
            object_id=obj.pk, field=field, item_date=date)

        processed_tags.append(tag)
    return processed_tags

def _getEntityText(key):
    """
    Try to resolve the entity given the key
    """
    if settings.RESOLVE_KEYS:
        try:
            r = SuperTag.objects.get(pk=key)
            return r.name
        except SuperTag.DoesNotExist:
            return key

    return key
