"""
This is where you can write a lot of the code that responds to URLS - such as a page request from a browser
or a HTTP request from another application.

From here you can follow the Cantemo Portal Developers documentation for specific code, or for generic
framework code refer to the Django developers documentation.

"""

import logging
import os
import time
from django.urls import reverse_lazy
from django.core.exceptions import ObjectDoesNotExist
from django.http import (
    HttpResponse,
    HttpResponseRedirect,
    HttpResponseNotFound,
)
from django.forms import modelformset_factory

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
from portal.plugins.TapelessIngest.models.clip import (
    REST_EXTRA_COMPONENT_WAIT_SECONDS,
    REST_COMPONENT_WAIT_BUDGET_SECONDS,
    Clip,
)
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.models.settings import (
    Settings,
    MetadataMapping,
)
from portal.plugins.TapelessIngest.forms import (
    SettingsForm,
    MetadataMappingForm,
)
from portal.plugins.TapelessIngest.serializers import (
    ClipSerializer,
    FolderSerializer,
)

log = logging.getLogger(__name__)

# Serializer fields that DELIBERATELY have no `Folder` model field behind
# them, and are therefore dropped from `Folder(**validated_data)` on every
# request. Named here so the "dropped" log line below can stay a signal:
# it fires for the fields nobody declared, i.e. the typos.
SERIALIZER_ONLY_FOLDER_FIELDS = frozenset({"error"})


class SettingsView(CView):
    template_name = "TapelessIngest/admin/settings.html"
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

        MetadatasMappingsFormset = modelformset_factory(
            MetadataMapping, form=MetadataMappingForm, can_delete=True, extra=0
        )
        metadatas_form = MetadatasMappingsFormset(prefix="metadata")

        if hasattr(ti_settings, "storage_id"):
            _tip = TapelessIngestPath(ti_settings.storage_id, "")
            storage_root_path = _tip.root_path
        else:
            storage_root_path = ""

        return Response(
            {
                "settings_form": settings_form,
                "metadatas_form": metadatas_form,
                "storage_root_path": storage_root_path,
            }
        )

    def post(self, request):
        # Build settings
        try:
            ti_settings = Settings.objects.get(pk=1)
        except ObjectDoesNotExist:
            ti_settings = Settings(pk=1)
            ti_settings.save()
        settings_form = SettingsForm(
            request.POST, instance=ti_settings, prefix="settings"
        )

        MetadatasMappingsFormset = modelformset_factory(
            MetadataMapping, form=MetadataMappingForm, can_delete=True, extra=0
        )
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
            return Response(
                {
                    "settings_form": settings_form,
                    "metadatas_form": metadatas_form,
                    "storage_root_path": storage_root_path,
                }
            )


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
            return Response(
                {"error": "no file id in request"}, status=status.HTTP_200_OK
            )
        if action is None or action != "NEW":
            return Response({}, status=status.HTTP_200_OK)

        from portal.vidispine.istorage import StorageHelper

        sth = StorageHelper()
        _file = sth.getFileById(file_id)
        file_path = _file.getPath()

        log.info(
            f"File {file_id} have been added to storage {storage_id} with {file_path}"
        )

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
            # Two pre-existing defects, both fixed here because the first
            # request-level test ever written against this view hit them
            # immediately and neither branch below can run without it.
            #
            # 1. An INVALID body used to fall straight through, leaving
            #    `folder` unbound and answering 500 on an UnboundLocalError
            #    where the client's own payload was the problem. It is a
            #    400, and it says which fields.
            # 2. `FolderSerializer` declares `error` — required, and not
            #    blank-able — but `Folder` has NO `error` model field, so
            #    `Folder(**validated_data)` raised `TypeError` for every
            #    accepted body. Between the two, this endpoint could not
            #    succeed for ANY input. Only concrete model fields are
            #    passed to the constructor; the serializer's extra keys
            #    (`error`, and anything added to it later) are dropped.
            if not folder_serializer.is_valid():
                return Response(
                    folder_serializer.errors,
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # `concrete_fields`, NOT `get_fields()`: the latter also
            # returns REVERSE relations (`Clip.folders` is listed on
            # `Folder` under the field name `clip`, and anything else
            # pointing at `Folder` would be too), which are not
            # constructor keywords at all. `Model.__init__` accepts any
            # name `get_field()` resolves, so a serializer key colliding
            # with a reverse relation's name would have gone through
            # `Folder(**kwargs)` and landed as a stray attribute on the
            # instance — never a model field, and never said.
            #
            # `validated_data` is keyed by SOURCE, not by declared name:
            # `umid = CharField(source="id")` arrives here as `id`. The
            # filter and `SERIALIZER_ONLY_FOLDER_FIELDS` both speak in
            # sources.
            model_fields = {f.name for f in Folder._meta.concrete_fields}
            folder_kwargs = {
                key: value
                for key, value in folder_serializer.validated_data.items()
                if key in model_fields
            }
            dropped = sorted(
                set(folder_serializer.validated_data)
                - set(folder_kwargs)
                - SERIALIZER_ONLY_FOLDER_FIELDS
            )
            if dropped:
                # SAID, not swallowed. A serializer field with no model
                # field behind it is either deliberate (`error`) or a
                # typo, and dropping both silently makes the two
                # indistinguishable — which is how `error` came to raise
                # TypeError here unnoticed in the first place.
                #
                # The DELIBERATE ones are subtracted first. `error` is
                # dropped on every single request, so logging it made the
                # line fire unconditionally — which is the same as not
                # having it: the typo it exists to surface was buried in
                # a message the operator learns to ignore.
                log.info(
                    "ClipsInPathsView: folder field(s) %s are on the "
                    "serializer but not on the Folder model, so they are not "
                    "passed to it",
                    ", ".join(dropped),
                )
            folder = Folder(**folder_kwargs)

            if request.data["clips"] == "__all__":
                # The SHORT per-clip bound, AND the shared budget it is
                # clamped against. The cron gets the generous default and
                # no budget at all; a caller sitting on an HTTP socket
                # gets both.
                #
                # A DURATION, not a deadline: `Folder.ingest` runs a full
                # scan pass before it imports anything, and it opens the
                # deadline at the start of its INGEST leg. Opening it
                # here would let discovery on a large healthy folder eat
                # the budget and hand every clip a 0 s bound.
                response = folder.ingest(
                    user=request.user,
                    component_wait_seconds=REST_EXTRA_COMPONENT_WAIT_SECONDS,
                    component_wait_budget=REST_COMPONENT_WAIT_BUDGET_SECONDS,
                )
                new_clips = response["clips"]
            else:
                serialized_clips = request.data["clips"]
                # This branch has no scan pass to precede it, so the
                # ingest leg starts here and so does the budget.
                component_wait_deadline = (
                    time.monotonic() + REST_COMPONENT_WAIT_BUDGET_SECONDS
                )
                for serialized_clip in serialized_clips:
                    serializer = ClipSerializer(data=serialized_clip)
                    if serializer.is_valid():
                        clip = Clip(**serializer.validated_data)
                        try:
                            result = clip.ingest(
                                user=request.user,
                                collection_id=folder.collection_id,
                                folder=folder,
                                # This clip comes from the request body,
                                # not from a scan: having no row yet is
                                # normal here, not the broken invariant
                                # Clip.ingest logs for the scan path.
                                expect_persisted=False,
                                # The short bound and the request's own
                                # budget: see the folder branch above.
                                # This loop is N clips on ONE held
                                # request thread, which is exactly what
                                # the deadline exists to bound.
                                component_wait_seconds=(
                                    REST_EXTRA_COMPONENT_WAIT_SECONDS
                                ),
                                component_wait_deadline=component_wait_deadline,
                            )
                            # A clip that came back `failed` without
                            # raising said nothing here at all: this is
                            # the entry point that just gained a bound
                            # and a budget, and it was the one reporting
                            # nothing about why a clip did not ingest.
                            # `clip.error` carries the reason
                            # `import_file` recorded; the response shape
                            # is an API contract this story does not
                            # touch, so the reason goes to `portal.log`
                            # and onto the clip the serializer returns.
                            if result.get("failed"):
                                log.error(
                                    f"Failed to ingest clip {clip} from the "
                                    f"REST endpoint: "
                                    f"{clip.error or 'no reason recorded'}"
                                )
                            # The serializer's writable `metadatas` field
                            # used to be persisted by Clip.save()'s
                            # per-key fan-out, deleted in story 2.4. This
                            # is the same rows, batched (models/clip.py).
                            # persist_metadatas() is atomic, so a failure
                            # rolls back to a savepoint instead of
                            # poisoning this request's transaction.
                            clip.persist_metadatas()
                        except Exception as e:
                            log.error(
                                f"Error ingesting clip {clip} from the REST "
                                f"endpoint: {e}",
                                exc_info=True,
                            )
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
            page = request.data["page"]
        if "number" not in list(request.data.keys()):
            number = 25
        else:
            number = request.data["number"]
        if "cursor" not in list(request.data.keys()):
            cursor = None
        else:
            cursor = request.data["cursor"]

        first = (page - 1) * number

        clips = []
        subfolders = []
        hits = 0

        for path in paths:
            folder, is_new = Folder.get_or_new(
                path=path["path"], storage_id=path["storage"]
            )
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
            "next": page + 1,
            "pages": hits / number,
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


class ClipsByItemView(APIView):
    """API endpoint to get clips associated with a specific item"""

    renderer_classes = (JSONRenderer,)
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, item_id):
        try:
            clips = Clip.objects.filter(item_id=item_id)
            serializer = ClipSerializer(clips, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


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
                    bufsize=10**8,
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

    template_name = "TapelessIngest/proxy_player.html"

    def __call__(self):
        ctx = {}
        if "clip_id" in self.kwargs:
            clip = Clip.objects.get(pk=self.kwargs["clip_id"])

            ctx["clip"] = clip
            ctx["mimetype"] = "video/mp4"

            return self.main(self.request, self.template, ctx)
