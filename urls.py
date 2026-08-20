"""

"""
from django.urls import re_path

from rest_framework import routers

from . import views

# This new app handles the request to the URL by responding with the view which is loaded
# from portal.plugins.TapelessIngest.views.py. Inside that file is a class which responsedxs to the
# request, and sends in the arguments template - the html file to view.
# name is shortcut name for the urls.

router = routers.SimpleRouter()

urlpatterns = [
    re_path(r"^api/browser/clips$", views.ClipsInPathsView.as_view()),
    re_path(r"^api/browser/clips/jobs$", views.ClipsJobsProgress.as_view()),
    re_path(r"^api/item/(?P<item_id>.*)/clips$", views.ClipsByItemView.as_view(), name="clips_by_item"),
    re_path(
        r"^file/(?P<file_id>.*)/thumbnail$",
        views.getFileThumbnail,
        kwargs={},
        name="file_thumbnail",
    ),
    re_path(
        r"^notification/file/created$", views.FileNotificationView.as_view()
    ),  # Vidispine notification VX-591
    re_path(
        r"^admin/$",
        (views.SettingsView.as_view()),
        name="settings",
    ),
    re_path(
        r"^clips/(?P<clip_id>.*)/thumbnail$",
        views.getClipThumbnail,
        name="clip_thumbnail",
    ),
    re_path(
        r"^clips/(?P<clip_id>.*)/proxy$",
        views.getClipProxy,
        name="clip_proxy",
    ),
    re_path(
        r"^clips/(?P<clip_id>.*)/preview$",
        views.clipPreview,
        name="clip_preview",
    ),
]

urlpatterns += router.urls
