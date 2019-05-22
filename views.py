"""
This is where you can write a lot of the code that responds to URLS - such as a page request from a browser
or a HTTP request from another application.

From here you can follow the Cantemo Portal Developers documentation for specific code, or for generic
framework code refer to the Django developers documentation.

"""
import logging
from urllib import quote_plus

from django.core.cache import cache

from django.utils.translation import ugettext as _
from django.utils.translation import ungettext

from django.shortcuts import redirect

from django.contrib.auth.decorators import login_required

from rest_framework import viewsets, permissions
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.parsers import JSONParser
from rest_framework.views import APIView
from rest_framework.decorators import action, link
from rest_framework.response import Response

from portal.generic.baseviews import ClassView, CView
from portal.vidispine.iexception import NotFoundError

import json
import os

from django.db.models import Q

from django.conf import settings

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseNotFound
from django.http import HttpResponse

from portal.plugins.TapelessIngest.utilities import getProvidersList, getDefaultProvider, getProvidersByName, update_collection_tags
from portal.plugins.TapelessIngest.storage_utilities import getStorageUri
from portal.plugins.TapelessIngest.models import Clip, MetadataMapping, Folder, Settings, TapelessStorage, TapelessStoragePath
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
            from portal.vidispine.iuser import UserHelper

            ch = CollectionHelper(runas=self.request.user)
            uh = UserHelper(runas=self.request.user)
            items = self.request.POST.getlist('selected_objects')
            _collections_ = self.request.POST.get('collection', '')
            _tags_ = self.request.POST.get('tags', None)
            collections, new_collections = parse_collections_str(_collections_)
            tags = _tags_.split('*valsep*')
            # Remove empty string from tags
            tags = [tag for tag in tags if tag != '']
            collectionprofilegroup = self.request.POST.get('collectionprofilegroup')

            errorlist = []
            if 'newcollectionname' in self.request.POST:
                new_collections.append(self.request.POST['newcollectionname'])
            failed = []
            settingsprofile_id = uh.getUserSettingsProfile(collectionprofilegroup)
            parentCollection_id = 'VX-1511'
            if len(new_collections) > 0:
                for new_collection_name in new_collections:
                    new_collection = ch.createCollection(quote_plus(new_collection_name.replace('*-new-*', '').encode('utf8')), settingsprofile_id)
                    ch.addCollectionToCollection(parentCollection_id, new_collection.getId())
                    collections.append(new_collection.getId())
                    errorlist.append('Failed to create collections %s' % str(failed))

            if _tags_ is not None:
                for collection in collections:
                    log.info('Update tags: %s for collections %s' % (str(tags), str(collection)))
                    update_collection_tags(collection, tags)
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

        if self.request.method != "POST":
            return HttpResponseNotFound('<h1>Only POST requests are valid</h1>')

        # settings = Settings.objects.get(pk=1)

        # base_path = settings.base_folder

        storages = {
            'Infortrend': '/Volumes/Infortrend/AA - RUSHES TAPELESS',
            'PAD_Storage': '/Volumes/PAD_Storage/AA - RUSHES TAPELESS',
        }

        ctx = {}
        # create a form instance and populate it with data from the request:
        # check whether it's not empty:
        if self.request.POST['path'] != 'null':
            path = self.request.POST['path']
        else:
            path = cache.get('tapeless_ingest_%s_folder_path' % self.request.user.id, '.')

        folders = {}

        # If the root is requested, return all storages
        if path == '.':
            for storage, storage_path in storages.iteritems():
                folders[storage] = {
                    'path': storage_path
                }
            parent = {"name":"storages"}
        else:

            invalid_path = True

            parent = {
                "name":path.rsplit('/',1)[1],
                "path": path.rsplit('/',1)[0]
            }

            for storage, storage_path in storages.iteritems():
                if storage_path in path:
                    invalid_path = False
                    if path in storage_path:
                        parent['path'] = '.'

            if invalid_path:
                ctx['message'] = "You can not navigate in this path"
                return self.main(self.request, self.template, ctx)

            folders_names = [ f for f in os.listdir(path) if os.path.isdir(os.path.join(path,f)) ]

            for folder_name in folders_names:
                folder_path = os.path.join(path, folder_name)
                folders[folder_name] = {
                    "path": folder_path
                }

        cache.set('tapeless_ingest_%s_folder_path' % self.request.user.id, path)

        ctx['parent'] = parent
        ctx['folders'] = sorted(folders.items())
        ctx['current_path'] = path

        # return a response to the request
        return self.main(self.request, self.template, ctx)


# setup the object, and decorate so that only logged in users can see it
IngestFileNavigationView = IngestFileNavigationView._decorate(login_required)

class FileNavigationView(APIView):
    renderer_classes = (JSONRenderer, )
    parser_classes = (JSONParser,  )

    def put(self, request, format=None):
        settings = Settings.objects.get(pk=1)
        base_path = settings.base_folder
        storages = {
            'Infortrend': '/Volumes/Infortrend/AA - RUSHES TAPELESS',
            'PAD_Storage': '/Volumes/PAD_Storage/AA - RUSHES TAPELESS',
        }

        if "storage" not in request.DATA.keys():
            fodlers = []
            for storage, storage_path in storages:
                folders.append({
                    'storage': storage,
                    'folder_name': storage,
                    'folder_path': '.'
                })
            datas = {
                'current_folder': {'name': 'Storages', 'path': '.'},
                'parent_folder': {'name': 'Storages', 'path': '.'},
                'folders': folders
            }
            return Response(data=datas)
        else:
            if request.DATA['storage'] in storages.keys():
                base_path = request.DATA['storage']
            else:
                return Response("Invalid storage", status=status.HTTP_400_BAD_REQUEST)

        if "path" not in request.DATA.keys():
            path = '.'
        else:
            path = os.path.normpath(request.DATA['path'])
            absolute_path = os.path.normpath(os.path.join(base_path, path))

        if base_path not in absolute_path:
            return Response('You can only navigate in %s' % os.path.basename(base_path), status=status.HTTP_403_FORBIDDEN)
        if os.path.isdir(absolute_path) == False:
            return Response('The path %s doesn\'t exists' % os.path.join(os.path.basename(base_path), path), status=status.HTTP_404_NOT_FOUND)
        current_folder = {
            "name": os.path.basename(absolute_path),
            "path": path
        }
        if absolute_path != base_path:
            parent_path = os.path.normpath(os.path.join(path, os.pardir))
            parent_folder = {
                "name":os.path.basename(parent_path),
                "path":parent_path
            }
        else:
            parent_folder = current_folder
        folders_names = [ f for f in os.listdir(absolute_path) if os.path.isdir(os.path.join(absolute_path,f)) ]
        folders = []
        for folder_name in folders_names:
            folders.append({
                'storage': storage,
                'folder_name': folder_name,
                'folder_path': os.path.join(path, folder_name)
            })
        datas = {
            'current_folder': current_folder,
            'parent_folder': parent_folder,
            'folders': folders
        }
        return Response(data=datas)

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
                image_data = open('/srv/thumbnail/no-thumbnail.jpg', "rb").read()
                return HttpResponse(image_data, mimetype="image/jpeg")

class getClipProxy(ClassView):

    def __call__(self):
        ctx = {}
        if 'clip_id' in self.kwargs:

            from django.http import HttpResponse
            from os.path import isfile

            clip = Clip.objects.get(pk=self.kwargs['clip_id'])

            video_data, mimetype = clip.provider.getProxy(clip)

            from django.core.servers.basehttp import FileWrapper



            if isfile(video_data):
                log.debug("Trying to render %s with mime %s" % (video_data, mimetype))

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
    renderer_classes = (JSONRenderer,)
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
        log.debug("Actualisation for clip %s, new status is %s" % (clip.umid, status))
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
    renderer_classes = (JSONRenderer,)
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

            content = JSONRenderer().render(data)

            return HttpResponse(content, status=201)
        else:
            return self.main(self.request, self.template, extra_context)


UserClipsView = ClipsView._decorate(login_required)



class TapelessStoragePathViewSet(viewsets.ModelViewSet):
    serializer_class = TapelessStoragePathSerializer
    renderer_classes = (JSONRenderer,)
    permission_classes = [permissions.IsAuthenticated]
    queryset = TapelessStoragePath.objects.all()

class TapelessStorageViewSet(viewsets.ModelViewSet):
    serializer_class = TapelessStorageSerializer
    renderer_classes = (JSONRenderer,)
    permission_classes = [permissions.IsAuthenticated]
    queryset = TapelessStorage.objects.all()

    def create(self, request):
        serialized = self.serializer_class(data=request.DATA)
        if serialized.is_valid():
            serialized.save()
            return Response(status=HTTP_202_ACCEPTED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

"""
    def update(self, instance, validated_data):
        # Update the  instance
        instance.some_field = validated_data['some_field']
        instance.save()

        # Delete any detail not included in the request
        path_ids = [item['owner_id'] for item in validated_data['paths']]
        for path in cars.owners.all():
            if owner.id not in path_ids:
                owner.delete()

        # Create or update owner
        for owner in validated_data['owners']:
            ownerObj = Owner.objects.get(pk=item['id'])
            if ownerObje:
                ownerObj.some_field=item['some_field']
                ....fields...
            else:
               ownerObj = Owner.create(car=instance,**owner)
            ownerObj.save()

        return instance
"""
