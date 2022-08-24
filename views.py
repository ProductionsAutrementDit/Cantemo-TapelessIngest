"""
This is where you can write a lot of the code that responds to URLS - such as a page request from a browser
or a HTTP request from another application.

From here you can follow the Cantemo Portal Developers documentation for specific code, or for generic
framework code refer to the Django developers documentation.

"""
import logging
import os

from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseNotFound
from django.forms import modelformset_factory
from django.contrib.auth.decorators import login_required

from rest_framework import permissions
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.parsers import JSONParser
from rest_framework.views import APIView
from rest_framework.response import Response

from portal.generic.baseviews import CView, ClassView
from portal.generic.decorators import isAdminPermission
from portal.vidispine.iexception import NotFoundError

from portal.plugins.TapelessIngest.helpers import TapelessIngestPath
from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.models.settings import Settings, MetadataMapping
from portal.plugins.TapelessIngest.forms import SettingsForm, MetadataMappingForm
from portal.plugins.TapelessIngest.serializers import ClipSerializer, FolderSerializer

log = logging.getLogger(__name__)


class SettingsView(CView):
    template_name = 'TapelessIngest/admin/settings.html'
    # roles = ['portal_system_transcode_profile_read']
    permission_classes = (isAdminPermission,)

    def get(self, request):
        # Build settings
        try:
            ti_settings = Settings.objects.get(pk=1)
        except ObjectDoesNotExist:
            ti_settings = Settings(pk=1)
            ti_settings.save()
        settings_form = SettingsForm(instance=ti_settings, prefix="settings")

        MetadatasMappingsFormset = modelformset_factory(MetadataMapping, form=MetadataMappingForm, can_delete=True, extra=0)
        metadatas_form = MetadatasMappingsFormset(prefix="metadata")

        if hasattr(ti_settings, "storage_id"):
            _tip = TapelessIngestPath(ti_settings.storage_id, "")
            storage_root_path = _tip.root_path
        else:
            storage_root_path = ""

        return Response({
            "settings_form": settings_form,
            "metadatas_form": metadatas_form,
            "storage_root_path": storage_root_path
        })

    def post(self, request):
        # Build settings
        try:
            ti_settings = Settings.objects.get(pk=1)
        except ObjectDoesNotExist:
            ti_settings = Settings(pk=1)
            ti_settings.save()
        settings_form = SettingsForm(request.POST, instance=ti_settings, prefix="settings")

        MetadatasMappingsFormset = modelformset_factory(MetadataMapping, form=MetadataMappingForm, can_delete=True, extra=0)
        metadatas_form = MetadatasMappingsFormset(request.POST, prefix="metadata")

        if hasattr(ti_settings, "storage_id"):
            _tip = TapelessIngestPath(ti_settings.storage_id, "")
            storage_root_path = _tip.root_path
        else:
            storage_root_path = ""

        if settings_form.is_valid() and metadatas_form.is_valid():
            settings_form.save()
            metadatas_form.save()
            return HttpResponseRedirect(reverse_lazy("tapelessingest:settings"))
        else:
            return Response({
                "settings_form": settings_form,
                "metadatas_form": metadatas_form,
                "storage_root_path": storage_root_path
            })
            

class FileNotificationView(APIView):
    """
    Get new files notifications from Vidispine
    """

    permission_classes = (permissions.AllowAny,)
    renderer_classes = (JSONRenderer,)

    def get(self, request):
        return Response({"test": "test"})

    def post(self, request):

        data = request.data
        log.info(f"request from storage ha been received: {data}")

        file_id = None
        action = None
        storage_id = None

        if "field" not in list(request.data.keys()):
            return Response({}, status=status.HTTP_200_OK)

        for field in request.data["field"]:
            if field["key"] == "fileId":
                file_id = field["value"]
            if field["key"] == "action":
                action = field["value"]
            if field["key"] == "storageId":
                storage_id = field["value"]
            if field["key"] == "itemId":
                item_id = field["value"]
            if field["key"] == "shapeTag":
                shape_tag = field["value"]

        if file_id is None:
            return Response({"error": "no file id in request"}, status=status.HTTP_200_OK)
        if action is None or action != "NEW":
            return Response({}, status=status.HTTP_200_OK)

        from portal.vidispine.istorage import StorageHelper

        sth = StorageHelper()
        _file = sth.getFileById(file_id)
        file_path = _file.getPath()

        log.info(f"File {file_id} have been added to storage {storage_id} with {file_path}")

        return Response({"ok"}, status=status.HTTP_200_OK)


class ClipsInPathsView(APIView):
    renderer_classes = (JSONRenderer,)
    parser_classes = (JSONParser,)

    def post(self, request):
        if "clips" not in list(request.data.keys()):
            return Response(
                "You have to provide at least one clip",
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        if "folder" not in list(request.data.keys()):
            return Response(
                "You have to provide a folder",
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_clips = []
        errors = []
        try:
            serialized_folder = request.data["folder"]
            folder_serializer = FolderSerializer(data=serialized_folder)
            if folder_serializer.is_valid():
                folder = Folder(**folder_serializer.validated_data)
            
            if request.data["clips"] == "__all__":
                new_clips = folder.ingest(user=request.user)
            else:
                serialized_clips = request.data["clips"]
                for serialized_clip in serialized_clips:
                    serializer = ClipSerializer(data=serialized_clip)
                    if serializer.is_valid():
                        clip = Clip(**serializer.validated_data)
                        try:
                            clip.ingest(user=request.user, collection_id=folder.collection_id, folder=folder)
                        except Exception as e:
                            clip.error = "%s" % e
                        new_clips.append(clip)
                    else:
                        errors.append({"SerializationError": serializer.errors})
            newSerializer = ClipSerializer(new_clips, many=True)
            return Response(newSerializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            errors.append("%s" % e)
            return Response(
                {"errors": errors},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def put(self, request):

        if "paths" not in list(request.data.keys()):
            return Response(
                "You have to provide at least one path",
                status=status.HTTP_204_NO_CONTENT,
            )
        paths = request.data["paths"]
        if "page" not in list(request.data.keys()):
            page = 1
        else:
            page = request.data['page']
        if "number" not in list(request.data.keys()):
            number = 25
        else:
            number = request.data['number']
        if "cursor" not in list(request.data.keys()):
            cursor = None
        else:
            cursor = request.data['cursor']

        first = (page - 1) * number

        clips = []
        subfolders = []
        hits = 0

        for path in paths:
            folder, is_new = Folder.get_or_new(path=path["path"], storage_id=path["storage"])
            subfolders += folder.getSubfolders(request.user)
            response = folder.scan(first=first, number=number, cursor=cursor)
            clips += response["clips"]
            hits += response["hits"]

        serialized_folder = FolderSerializer(folder)
        serialized_clips = ClipSerializer(clips, many=True)
        serialized_subfolders = FolderSerializer(subfolders, many=True)

        datas = {
            "folder": serialized_folder.data,
            "clips": serialized_clips.data,
            "subfolders": serialized_subfolders.data,
            "paths": paths,
            "hits": hits,
            "page": page,
            "number": number,
            "next": page+1,
            "pages": hits/number
        }

        return Response(data=datas)


class ClipsJobsProgress(APIView):
    renderer_classes = (JSONRenderer,)
    parser_classes = (JSONParser,)

    def put(self, request):
        if "jobs_ids" not in list(request.data.keys()):
            return Response(
                "You have to provide at least one job id",
                status=status.HTTP_204_NO_CONTENT,
            )
        from portal.vidispine.ijob import JobHelper

        _jh = JobHelper(runas=request.user)

        datas = {}
        for job_id in request.data["jobs_ids"]:
            if job_id is None:
                continue
            try:
                job = _jh.getJob(job_id)
                datas[job_id] = {
                    "id": job.getId(),
                    "progress": job.getProgress(),
                    "status": job.getStatus(),
                    "type": job.getType(),
                }
            except NotFoundError:
                datas[job_id] = {
                    "id": job_id,
                    "progress": 0,
                    "status": "NOT_FOUND",
                    "type": "UNKNOWN",
                }

            # progress = jobs[0].getProgress()

        return Response(data=datas)


class getFileThumbnail(ClassView):
    def __call__(self):
        if "file_id" in self.kwargs:

            file_id = self.kwargs["file_id"]

            from os.path import isfile
            import subprocess as sp
            from portal.vidispine.istorage import StorageHelper

            FFMPEG_BIN = "/usr/bin/ffmpeg"

            _sh = StorageHelper()
            file = _sh.getFileById(file_id)

            path = file.getPath()
            storage_id = file.getStorageId()
            storage = _sh.getStorage(storage_id)
            storage_methods = storage.getMethods()
            for s in storage_methods:
                if s.getBrowse():
                    root_path = s.getFirstURI()["url"]
            absolute_path = os.path.join(root_path, path)

            thumbnail = "/srv/thumbnail/files/%s.jpg" % file_id

            if not isfile(thumbnail):
                command = [
                    FFMPEG_BIN,
                    "-i",
                    absolute_path,
                    "-an",
                    "-s",
                    "400x222",
                    "-vframes",
                    "1",
                    thumbnail,
                ]
                process = sp.Popen(
                    command,
                    stdout=sp.PIPE,
                    stderr=sp.STDOUT,
                    bufsize=10 ** 8,
                )
                outs, errs = process.communicate()

                if not isfile(thumbnail):
                    image_data = open("/srv/thumbnail/no-thumbnail.jpg", "rb").read()
                    return HttpResponse(image_data, content_type="image/jpeg")

            image_data = open(thumbnail, "rb").read()
            return HttpResponse(image_data, content_type="image/jpg")


class getClipThumbnail(ClassView):
    def __call__(self):
        if "clip_id" in self.kwargs:

            from os.path import isfile

            clip = Clip.objects.get(pk=self.kwargs["clip_id"])

            thumbnail = clip.provider.getThumbnail(clip)

            if isfile(thumbnail):
                image_data = open(thumbnail, "rb").read()
                return HttpResponse(image_data, content_type="image/png")
            else:
                image_data = open("/srv/thumbnail/no-thumbnail.jpg", "rb").read()
                return HttpResponse(image_data, content_type="image/jpeg")


class getClipProxy(ClassView):
    def __call__(self):
        if "clip_id" in self.kwargs:

            from os.path import isfile

            clip = Clip.objects.get(pk=self.kwargs["clip_id"])

            video_data, mimetype = clip.provider.getProxy(clip)

            from wsgiref.util import FileWrapper

            if isfile(video_data):
                log.debug(f"Trying to render {video_data} with mime {mimetype}")

                file = FileWrapper(open(video_data, "rb"))

                response = HttpResponse(file, content_type=mimetype)
                response["Content-Length"] = os.path.getsize(video_data)
                return response
            else:
                return HttpResponseNotFound("<h1>This clip has no proxy</h1>")


class clipPreview(ClassView):
    def __call__(self):
        ctx = {}
        if "clip_id" in self.kwargs:

            clip = Clip.objects.get(pk=self.kwargs["clip_id"])

            ctx["clip"] = clip
            ctx["mimetype"] = "video/mp4"

            return self.main(self.request, self.template, ctx)
