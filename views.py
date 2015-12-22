"""
This is where you can write a lot of the code that responds to URLS - such as a page request from a browser
or a HTTP request from another application.

From here you can follow the Cantemo Portal Developers documentation for specific code, or for generic 
framework code refer to the Django developers documentation. 

"""
import logging
from django.utils.translation import ugettext as _
from django.utils.translation import ungettext

from django.shortcuts import redirect

from django.contrib.auth.decorators import login_required

from rest_framework import viewsets, permissions, renderers
from rest_framework.decorators import action, link
from rest_framework.response import Response

from portal.generic.baseviews import ClassView
from portal.vidispine.iexception import NotFoundError

import json
import os

from django.db.models import Q

from django.conf import settings

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseNotFound  
from django.http import HttpResponse

from portal.plugins.TapelessIngest.utilities import getProvidersList, getDefaultProvider, getProvidersByName
from portal.plugins.TapelessIngest.storage_utilities import getStorageUri
from portal.plugins.TapelessIngest.models import Clip, MetadataMapping, Folder, Settings
from portal.plugins.TapelessIngest.forms import SettingsForm
from portal.plugins.TapelessIngest.serializers import *

from portal.plugins.TapelessIngest.ingest import ingest_clip


log = logging.getLogger(__name__)


class AdminAppView(ClassView):
    """ Show the page. Add your python code here to show dynamic content or feed information in
        to external apps
    """
    def __call__(self):
        # __call__ responds to the incoming request. It will already have a information associated to it, such as self.template and self.request
        
        ctx = {}
        
        # return a response to the request
        return self.main(self.request, self.template, ctx)

# setup the object, and decorate so that only logged in users can see it
AdminAppView = AdminAppView._decorate(login_required)

class SettingsView(ClassView):

    def __call__(self):
        
        ctx = {}
        
        # Build the metadata mapping form
        import portal.plugins.TapelessIngest.providers.providers as Providers
        from portal.plugins.TapelessIngest.forms import MetadataMappingForm
        from django.forms.models import modelform_factory

        # Get all Metadata Mappings
        metadata_mappings = MetadataMapping.objects.all()
        
        # Build settings
        try:
            settings = Settings.objects.get(pk=1)
        except ObjectDoesNotExist:
            settings = Settings(pk=1)
            settings.save()

        if self.request.method == u'POST':
            settings_form = SettingsForm(self.request.POST, instance=settings, prefix='settings')
            metadatas_form = MetadataMappingForm(self.request.POST, prefix='metadata')

            if settings_form.is_valid():
                settings = settings_form.save()

            if metadatas_form.is_valid():
                instance = metadatas_form.save()
            else:
                metadatas_form = MetadataMappingForm(prefix='metadata')

        else:
            metadatas_form = MetadataMappingForm(prefix='metadata')
            settings_form = SettingsForm(instance=settings, prefix='settings')      
        
        from portal.vidispine.istorage import StorageHelper
            
        if settings.storage_id:
            storage_root_path = getStorageUri(settings.storage_id)
        else:
            storage_root_path = ""
        
        ctx = {u'settings_form': settings_form, u'metadatas_form': metadatas_form, u'storage_root_path': storage_root_path, u'metadatas': metadata_mappings}
        return self.main(self.request, self.template, ctx)

# setup the object, and decorate so that only logged in users can see it
SettingsView = SettingsView._decorate(login_required)

class AdminMetadatRemoveView(ClassView):
  
    def __call__(self):
        if ('metadata_id' in self.kwargs):
            try:
                MetadataMapping.objects.get(pk=self.kwargs['metadata_id']).delete()
            except MetadataMapping.DoesNotExist as e:
                pass
        return redirect('settings')

# setup the object, and decorate so that only logged in users can see it
AdminMetadatRemoveView = AdminMetadatRemoveView._decorate(login_required)

class GenericAppView(ClassView):
    """ Show the page. Add your python code here to show dynamic content or feed information in
        to external apps
    """
    def __call__(self):

        # __call__ responds to the incoming request. It will already have a information associated to it, such as self.template and self.request

        log.debug("%s Viewing page" % self.request.user)        
        # define template variable
        ctx = {}
        
        # return a response to the request
        return self.main(self.request, self.template, ctx)

# setup the object, and decorate so that only logged in users can see it
GenericAppView = GenericAppView._decorate(login_required)

class addTargetCollection(ClassView):
    """Adding Item(s) TO A collection"""

    def __call__(self):
        if self.request.method == 'POST':
        
            from portal.vidispine.utils.collection import parse_collections_str, create_new_collections
            from portal.vidispine.icollection import CollectionHelper
        
            ch = CollectionHelper(runas=self.request.user)
            items = self.request.POST.getlist('selected_objects')
            _collections_ = self.request.POST.get('collection', '')
            collections, new_collections = parse_collections_str(_collections_)
            collectionprofilegroup = self.request.POST.get('collectionprofilegroup')

            errorlist = []
            if 'newcollectionname' in self.request.POST:
                new_collections.append(self.request.POST['newcollectionname'])
            failed = []
            if len(new_collections) > 0:
                ok, failed = create_new_collections(new_collections, collectionprofilegroup, self.request.user)
                collections.extend(ok)
                errorlist.append('Failed to create collections %s' % str(failed))
            nocollectionerror = False
            multiplecollection = False
            if len(collections) == 0:
                nocollectionerror = True
                if failed:
                    return self.json_response({'error': _('Failed to create collections')}, 500)
                return self.json_response({'error': _('You need to choose a collection')}, 500)
            if len(items) == 0:
                extra_context = {'error': _('Could not add items to collection')}
            else:
                log.debug('Adding items %s to collections %s' % (str(items), str(collections)))
                oktext = ungettext('All Items are scheduled for adding to collection ', 'All Items are scheduled for adding to the collections', len(collections))
                   
                for umid in items:
                    clip = Clip.objects.get(pk=umid)
                    clip.collection_id = ",".join(collections)
                    clip.save()
                    
                return self.json_response({'success': oktext,
                 'reloadPage': True}, 200)

            extra_context = {'errorlist': errorlist,
             'nocollectionerror': nocollectionerror,
             'multiplecollection': multiplecollection}
        else:
            try:
                _items = self.request.GET.getlist('selected_objects')
            except KeyError:
                _items = []

            collections = []
            collectionprofiles = [ _g for _g in self.request.user.groups.all() if _g.name not in settings.IGNORE_GROUPS ]
            default_group = self.request.user.get_profile().default_group
            default_group_name = default_group.name if default_group else ''
            extra_context = {'collections': collections,
             'collectionprofiles': collectionprofiles,
             'default_group': default_group_name,
             'collection_selector': True,
             'items': _items}
        return self.main(self.request, self.template, extra_context)


addTargetCollection = addTargetCollection._decorate(login_required)

class IngestFileNavigationView(ClassView):

    def __call__(self):
      
        log.debug("%s Viewing popup" % self.request.user)
      
        from os import listdir
        from os.path import isdir, join

        settings = Settings.objects.get(pk=1)
        
        base_path = settings.base_folder
        
        path = base_path
        
        ctx = {}
        
        # Get path input post
        if self.request.method == "POST":
            # create a form instance and populate it with data from the request:
            # check whether it's not empty:
            if self.request.POST['path'] is not "":
                path = self.request.POST['path']
        
        if base_path not in path:
            path = base_path
            ctx['message'] = "You can only navigate in " + base_path
        
        parent = {"name":path.rsplit('/',1)[1]}
        
        if path != base_path:
            parent['path'] = path.rsplit('/',1)[0]
        
        folders = {}
        
        folders_names = [ f for f in listdir(path) if isdir(join(path,f)) ]
        
        for folder_name in folders_names:
            folders[folder_name] = path + "/" + folder_name
            


        
        ctx['parent'] = parent
        ctx['folders'] = sorted(folders.items())
        ctx['path'] = path
        
        # return a response to the request
        return self.main(self.request, self.template, ctx)
        

# setup the object, and decorate so that only logged in users can see it
IngestFileNavigationView = IngestFileNavigationView._decorate(login_required)

class MetadatasUpdateItemView(ClassView):
    def __call__(self):
        ctx = {}
        if 'clip_id' in self.kwargs:
            clip = Clip.objects.get(pk=self.kwargs['clip_id'])
            provider = clip.provider
            result = provider.update_item_metadatas(clip)
            ctx['result'] = result
        return self.main(self.request, self.template, ctx)

class getClipThumbnail(ClassView):

    def __call__(self):
        ctx = {}
        if 'clip_id' in self.kwargs:

            from django.http import HttpResponse
            from os.path import isfile
            
            clip = Clip.objects.get(pk=self.kwargs['clip_id'])
            
            thumbnail = clip.provider.getThumbnail(clip)
        
            if isfile(thumbnail):
                image_data = open(thumbnail, "rb").read()
        
                return HttpResponse(image_data, mimetype="image/png")
            else:
                return HttpResponseNotFound('<h1>This clip has no thumbnail</h1>')  

class getClipProxy(ClassView):

    def __call__(self):
        ctx = {}
        if 'clip_id' in self.kwargs:

            from django.http import HttpResponse
            from os.path import isfile
            
            clip = Clip.objects.get(pk=self.kwargs['clip_id'])
            
            video_data, mimetype = clip.provider.getProxy(clip)
            
            from django.core.servers.basehttp import FileWrapper
            
            log.info("Tryning to render %s with mime %s" % (video_data, mimetype))
            
            if isfile(video_data):
                file = FileWrapper(open(video_data, "rb"))
        
                response = HttpResponse(file, mimetype=mimetype)
                response['Content-Length'] = os.path.getsize(video_data)
                return response
            else:
                return HttpResponseNotFound('<h1>This clip has no proxy</h1>')  

class clipPreview(ClassView):

    def __call__(self):
        ctx = {}
        if 'clip_id' in self.kwargs:

            from django.http import HttpResponse
            from os.path import isfile
            
            clip = Clip.objects.get(pk=self.kwargs['clip_id'])
            
            ctx['clip'] = clip
            ctx['mimetype'] = "video/mp4"
        
            return self.main(self.request, self.template, ctx)


class ClipViewSet(viewsets.ModelViewSet):
    serializer_class = ClipSerializer
    renderer_classes = (renderers.JSONRenderer,)
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return Clip.objects.filter(Q(folders__user=self.request.user), (Q(spanned=True) & Q(master_clip=True)) | Q(spanned=False))
        

    
    @link()
    def ingest(self, request, pk=None):
        clip = self.get_object()
        status = ingest_clip([clip], self.request.user)
        log.info("Ingest scheduled for clip " + clip.umid)
        serializer = self.serializer_class(clip)
        return Response(serializer.data)
    
    @link()
    def actualize(self, request, pk):
        clip = self.get_object()
        status = clip.provider.getClipStatus(clip)
        log.info("Actualisation for clip %s, new status is %s" % (clip.umid, status))
        clip.status = status
        clip.save()
        serializer = self.serializer_class(clip)
        return Response(serializer.data)
        

    def processing(self, request, clips_ids):
        list_clips_ids = clips_ids.split("_")
        queryset = Clip.objects.filter(folders__user=self.request.user, pk__in=list_clips_ids)
        serializer = self.serializer_class(queryset)
        return Response(serializer.data)
    


class FolderViewSet(viewsets.ModelViewSet):
  
    serializer_class = FolderSerializer
    renderer_classes = (renderers.JSONRenderer,)
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Folder.objects.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        from os.path import isdir, basename
        request.DATA['user'] = request.user.id
        request.DATA['basename'] = basename(request.DATA['path'])
        return super(FolderViewSet, self).create(request, *args, **kwargs)

# View for datatable datas
class ClipsView(ClassView):
    """
    View is for bring back ALL clips
    if currentuser is set to true, use the Runas user locally.
    """

    def __call__(self):
        self.slug = self.kwargs.get('slug', None)
        self.currentuser = self.kwargs.get('currentuser', True)
        extra_context = {
          'currentuser': self.kwargs.get('currentuser', True),
          'ajax_source': '.'}
        if self.request.is_ajax():
        
          
        
            number = int(self.request.GET.get('iDisplayLength', self.request.user.get_profile().paginate_by))
            first = int(self.request.GET.get('iDisplayStart', 0))
            sort_order = self.request.GET.get('sSortDir_0', 'desc')

            
            _clips = Clip.objects.filter(Q(folders__user=self.request.user), (Q(spanned=True) & Q(master_clip=True)) | Q(spanned=False))[first:number]
            _totalres = Clip.objects.filter(Q(folders__user=self.request.user), (Q(spanned=True) & Q(master_clip=True)) | Q(spanned=False)).count()

            _pretty_clips = []
            for _c in _clips:
                
                serialized_clip = ClipSerializer(_c)
                _pretty_clips.append(serialized_clip.data)
            
            data = {}
            data['iTotalRecords'] = _totalres
            data['iTotalDisplayRecords'] = _totalres
            data['clips'] = _pretty_clips
            
            content = renderers.JSONRenderer().render(data)

            return HttpResponse(content, status=201)
        else:
            return self.main(self.request, self.template, extra_context)


UserClipsView = ClipsView._decorate(login_required)