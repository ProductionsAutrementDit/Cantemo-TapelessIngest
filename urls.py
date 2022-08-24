"""

"""

from django.conf.urls import url

from rest_framework import routers

from . import views

# This new app handles the request to the URL by responding with the view which is loaded
# from portal.plugins.TapelessIngest.views.py. Inside that file is a class which responsedxs to the
# request, and sends in the arguments template - the html file to view.
# name is shortcut name for the urls.

router = routers.SimpleRouter()

urlpatterns = [
    url(r"^api/browser/clips$", views.ClipsInPathsView.as_view()),
    url(r"^api/browser/clips/jobs$", views.ClipsJobsProgress.as_view()),
    url(
        r"^file/(?P<file_id>.*)/thumbnail$",
        views.getFileThumbnail,
        kwargs={},
        name="file_thumbnail",
    ),
    url(
        r"^notification/file/created$", views.FileNotificationView.as_view()
    ),  # Vidispine notification VX-591
    url(
        r"^admin/$",
        (views.SettingsView.as_view()),
        name="settings",
    ),
    url(
        r"^clips/(?P<clip_id>.*)/thumbnail$",
        views.getClipThumbnail,
        kwargs={},
        name="clip_thumbnail",
    ),
    url(
        r"^clips/(?P<clip_id>.*)/proxy$",
        views.getClipProxy,
        kwargs={},
        name="clip_proxy",
    ),
    url(
        r"^clips/(?P<clip_id>.*)/preview$",
        views.clipPreview,
        kwargs={"template": "TapelessIngest/proxy_player.html"},
        name="clip_preview",
    ),
]

urlpatterns += router.urls
