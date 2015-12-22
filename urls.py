"""

"""
import logging

from django.conf.urls.defaults import *

from rest_framework import routers

from views import ClipViewSet, FolderViewSet

# This new app handles the request to the URL by responding with the view which is loaded 
# from portal.plugins.TapelessIngest.views.py. Inside that file is a class which responsedxs to the 
# request, and sends in the arguments template - the html file to view.
# name is shortcut name for the urls.

router = routers.SimpleRouter()

router.register(r'clips', ClipViewSet, 'clips')
router.register(r'folders', FolderViewSet, 'folders')

urlpatterns = patterns('portal.plugins.TapelessIngest.views',
    url(r'^browser/$', 'IngestFileNavigationView', kwargs={'template': 'TapelessIngest/folders.html'}, name='browser'),
    url(r'^add_target_collection_form$', 'addTargetCollection', kwargs={'template': 'TapelessIngest/includes/collection_add_target_form.html'}, name='addTargetCollection'),
    url(r'^admin/$', 'SettingsView', kwargs={'template': 'TapelessIngest/admin/settings.html'}, name='settings'),
    url(r'^admin/metadata/(?P<metadata_id>\w+)/remove/$', 'AdminMetadatRemoveView', kwargs={'template': 'TapelessIngest/admin/index.html'}, name='metadata_remove'),
    url(r'^clips/multiples/(?P<clips_ids>.*)$', ClipViewSet.as_view({'get': 'processing'})),
    url(r'^clips/(?P<clip_id>.*)/thumbnail$', 'getClipThumbnail', kwargs={}, name='clip_thumbnail'),
    url(r'^clips/(?P<clip_id>.*)/proxy$', 'getClipProxy', kwargs={}, name='clip_proxy'),
    url(r'^clips/(?P<clip_id>.*)/preview$', 'clipPreview', kwargs={'template': 'TapelessIngest/proxy_player.html'}, name='clip_preview'),
    url(r'^clips/(?P<clip_id>.*)/metadatas/update-item$', 'MetadatasUpdateItemView', kwargs={'template': 'TapelessIngest/metadatas-update-item.html'}, name='metadatas_update_item'),
    url(r'^clips/datatable/$', 'UserClipsView', name='clips_datatable', kwargs={'template': 'TapelessIngest/clips_view.html',
       'currentuser': False}),
    url(r'^$', 'GenericAppView', kwargs={'template': 'TapelessIngest/index.html'}, name='index'),)

urlpatterns += router.urls