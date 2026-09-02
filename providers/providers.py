# coding: utf-8


import logging
import os
import re
from collections.abc import Mapping

from portal.vidispine.iitem import ItemHelper, IngestHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.iexception import NotFoundError, VSAPIError

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models.settings import (
    Settings,
    MetadataMapping,
)
from portal.plugins.TapelessIngest.scan.context import browse_root_path
from portal.plugins.TapelessIngest.utilities import build_nested

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The main media file's own essence contribution
# ---------------------------------------------------------------------------
#
# `Clip._import_multi_component` declares, up front, how many components
# the placeholder shape must expect before Vidispine will promote it.
# Every EXTRA file contributes one component of its own type; the MAIN
# file contributes whatever Vidispine's shape deduction extracts from it,
# which is normally a container component plus a video one.
#
# It is not always a video one. A source Vidispine cannot decode yields a
# `binaryComponent` instead: that satisfies the container slot and fills
# NO video slot, so a declaration that assumed otherwise leaves one video
# slot unfilled and the shape a placeholder for ever — no `original` tag,
# no transcode, no error anywhere (measured on 110/110 drop-frame-flagged
# `.R3D` files of the 2026 STARLUX shoot).
#
# Which sources those are is FORMAT knowledge, so the PROVIDER answers it
# and `models/clip.py` stays format-agnostic: a main-file dict may carry
# `MAIN_FILE_YIELDS_VIDEO`. Absent, it means True — which is BACKWARD
# COMPATIBILITY, not a claim that True is the safe answer. True is what
# every provider declared implicitly before this key existed, so a
# provider that never thought about the question keeps exactly today's
# count; it is emphatically not the harmless direction, because
# over-declaring is the SILENT failure above and under-declaring is the
# loud one (Vidispine refuses the anchor with `400 {"invalidInput":
# {"explanation": "No more components of that type is accepted", "value":
# "VIDEO_COMPONENT"}}`, measured 2026-08-31, and the clip is reported
# failed). Only a provider that can actually ANSWER the question should
# depart from the default.
#
# The key deliberately does NOT spell the reader's name: a dict dumped
# into a log line has to be unambiguous about which half of the pair it
# is, and `{'yields_video_component': False}` beside a function of the
# same name reads as a bound method that was not called.
MAIN_FILE_YIELDS_VIDEO = "main_file_yields_video_component"


def yields_video_component(main_file):
    """Whether ``main_file``'s own essence will fill a video slot.

    The default is ``True`` for BACKWARD COMPATIBILITY: a P2, XDCAM or
    Ikegami anchor does deduce, and ``True`` is what every anchor was
    counted as before this key existed. Anything that is not a mapping,
    and an explicit ``None`` ("the provider could not tell"), read as
    ``True`` for that reason and no other — this function does NOT claim
    ``True`` is the safe direction. The two errors are not symmetric and
    neither is safe: OVER-declaring (counting a video slot the anchor
    never fills) is defect A, which is SILENT — the shape stays a
    placeholder for ever with no error anywhere; UNDER-declaring is LOUD
    — Vidispine refuses the anchor with ``400 … VIDEO_COMPONENT`` and the
    clip is reported failed. The default therefore keeps the behaviour a
    caller already had rather than guessing on its behalf, and a provider
    that can genuinely answer the question is expected to say so.

    Any other value is coerced with ``bool``, so a provider answering
    ``0``/``""`` is taken at its word rather than crashing an import.
    """
    if not isinstance(main_file, Mapping):
        return True
    declared = main_file.get(MAIN_FILE_YIELDS_VIDEO, True)
    if declared is None:
        return True
    return bool(declared)


def main_file_verdict_is_unknown(main_file):
    """Whether the provider DECLARED that it could not tell.

    An explicit ``None`` under ``MAIN_FILE_YIELDS_VIDEO`` is a provider
    that has the question and no evidence to answer it — ``red`` on an
    unreadable ``Abs TC``. It is distinct from an ABSENT key, which is a
    provider that never had the question and keeps the backward-
    compatible ``True``. ``yields_video_component`` above folds both
    into ``True`` for the callers that only need A count (a diagnostic,
    a log line); the multi-component import reads THIS before declaring
    a budget, because a budget is a claim and a claim without evidence
    is the silent over-declaration this key exists to end (ruled
    2026-09-02, spec D6).
    """
    if not isinstance(main_file, Mapping):
        return False
    return (
        MAIN_FILE_YIELDS_VIDEO in main_file
        and main_file[MAIN_FILE_YIELDS_VIDEO] is None
    )


class Provider:
    def __init__(self, folder=None):
        self.name = "Provider Name"
        self.machine_name = "ProviderName"
        self.folder = folder
        self.MetadataMappingModel = None
        self.clips = {}
        self.clip_count = 0
        self.file_extensions = ()
        self.folders_to_ignore = []

    def getExtensions(self):
        """Filename suffixes this provider may claim (see the class docstring).

        An empty declaration means "unknown reach": the scan pre-filter
        then treats this provider as applicable to every file.
        """
        return []

    def getSegmentedExtensions(self):
        """Suffixes whose files may be SEGMENTS of one clip, not clips.

        A camera that splits a long take into ``X_001.EXT``,
        ``X_002.EXT`` … ``X_NNN.EXT`` writes N files for ONE clip. The
        scan groups them at assembly time (``models/clip.py``'s
        ``segment_role``): the ``_001`` file anchors the clip, its
        higher-numbered siblings are attached as extra media files by
        ``getClipAdditionalMediaFiles`` and are never probed as clip
        candidates of their own, and an increment whose ``_001`` anchor
        is missing from the directory is a per-file ERROR — an
        incomplete copy, not a clip.

        Declare the suffix in the EXACT CASE your runtime guard accepts,
        and never wider. Matching is case-SENSITIVE, deliberately: a
        declaration that reaches past your guard makes the scan suppress
        files you will then decline, and whichever provider claims the
        anchor instead has no way to re-attach them — the segments are
        simply never ingested. This is the opposite direction from
        ``getExtensions()``, which must be a SUPERSET of your guard:
        that one decides who is OFFERED a file, this one decides who is
        DENIED one.

        A declared suffix suppresses matching files for EVERY provider,
        not only for you — there is one clip per anchor, not one per
        interested provider. The scan limits the blast radius by
        consulting only the providers the extraction pre-filter found
        APPLICABLE to that filename, so you cannot suppress a file you
        would never have been offered; within that set, your declaration
        is binding on all of them.

        This mechanism is for one take's media split into numbered files
        in ONE place: one ``Clip`` row carrying N files. It is NOT
        ``Clip.spanned``/``getSpannedClips()``, which exist for a take
        split across SEVERAL PHYSICAL CARDS (P2, XDCAM) and produce N
        linked rows with one master. The two are complementary; declaring
        a segmented extension neither reads nor writes a spanned field.

        An empty declaration (the default) means every file with this
        provider's extensions stands alone.
        """
        return []

    def is_extension_guarded(self):
        """Whether ``getExtensions()`` bounds what this provider can claim.

        Return ``False`` when the runtime guard in ``getMetadatasFromFile``
        does not test the file extension — the card providers, whose guard
        is sidecar presence and therefore extension-agnostic. Such a
        provider is applicable to every file, and its own guard decides.
        """
        return True

    def getMetadatasFromFile(self, media_file, metadatas, context):
        """Contribute this provider's metadatas for ``media_file``.

        Contract, enforced by the scan pipeline:

        * RETURN THE METADATAS ONLY. ``context`` is an argument and is
          mutated in place; returning it (the pre-registry-v2 two-tuple)
          raises a ``TypeError`` in the merge loop.
        * Every applicable provider runs and its result is merged, so
          contributing means either mutating ``metadatas`` in place or
          returning a mapping of the keys you own. Returning ``None``
          contributes nothing.
        * ``getExtensions()`` MUST be a SUPERSET of the guard used here.
          If the guard is not extension-based, override
          ``is_extension_guarded()`` to return ``False`` instead of
          widening the declaration.
        * Overwriting ``umid`` or ``provider`` once another provider has
          set them changes the clip's primary key; the merge loop logs an
          error when it happens.

        The base implementation contributes nothing.
        """
        return metadatas

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getClipStatus(self, clip):
        status = 0
        # Get the clip status
        if clip.item_id not in [None, ""]:
            _ith = ItemHelper()
            try:
                item = _ith.getItem(clip.item_id)
                if not item.isPlaceholder():
                    status = 4
                else:
                    status = 3
            except NotFoundError:
                log.warning(
                    "Item %s not found, resetting item_id for clip %s",
                    clip.item_id,
                    clip.umid,
                )
                clip.item_id = ""
            except VSAPIError as e:
                log.error("Vidispine API error checking item %s: %s", clip.item_id, e)
                clip.item_id = ""
            except Exception as e:
                log.error(
                    "Unexpected error checking clip status for %s: %s",
                    clip.umid,
                    e,
                    exc_info=True,
                )
                clip.item_id = ""

        if clip.item_id in [None, ""]:
            if clip.file_id not in [None, ""]:
                status = 2
            else:
                if clip.output_file not in [None, ""] and os.path.isfile(
                    clip.output_file
                ):
                    status = 1
                else:
                    status = 0

        return status

    def setSpannedClips(self, clips):
        pass

    def isMasterClip(self, clip):
        return True

    def isSpannedClip(self, clip):
        return False

    def isSpannedClipComplete(self, clip):
        return True

    def get_file_absolute_path(self, file, context=None):
        # Context-first: the run's ScanContext resolves the root with zero
        # storage HTTP calls. The per-call resolution below is the fallback
        # for callers that have no context, and costs one call per file.
        if context is not None:
            scan_context = context.get("scan_context")
            if scan_context is not None:
                absolute_path = scan_context.absolute_path_for(
                    file.getStorage(), file.getPath()
                )
                if absolute_path is not None:
                    return absolute_path
        sth = StorageHelper()
        storage_id = file.getStorage()
        storage = sth.getStorage(storage_id)
        root_path = browse_root_path(storage)
        if root_path is None:
            # Unresolvable root: fail HERE, like the pre-2.1 code failed at
            # storage.getMethods() — never let None flow onward into the
            # callers' os.path.dirname(...).
            raise AttributeError(
                f"storage {storage_id} has no browse-capable method "
                f"(no root path for {file.getPath()})"
            )
        return os.path.join(root_path, file.getPath())

    def probe_is_file(self, path, context=None):
        """Sidecar existence probe, answered from the scan's listings.

        When the provider context carries the scan's ``FolderListings``,
        a probe into a directory the scan already listed is answered from
        that listing, and a probe into an unlisted directory (a parent
        such as ``../MEDIAPRO.XML``, ``../CLIP/…``, ``../CLIPINF/…``)
        lazily lists THAT directory once and caches it for the rest of
        the invocation. A PRESENT sidecar then costs no filesystem call
        at all; an ABSENT one still costs one confirming ``stat`` per
        probe, because a membership miss is confirmed against the real
        filesystem before answering False.

        The probe is SPECULATIVE and says so (``probe=True``): a card
        provider guarded on sidecar presence probes for the layout it
        knows on every file it is offered, so the sibling directory it
        looks in (``../CLIP``, ``../CLIPINF``) legitimately does not
        exist on a card of any other type. That is an ANSWER — "not my
        card" — and must not land in the folder's errors. A sibling
        directory that exists but cannot be READ still does.

        ``FolderListings`` normalizes internally, so callers keep passing
        their un-normalized path (in xdcam it doubles as a cache key).
        Without listings this is exactly ``os.path.isfile``.
        """
        if context:
            listings = context.get("listings")
            if listings is not None:
                if os.path.isabs(path):
                    return listings.is_file(path, probe=True)
                # The verification API refuses relative paths by contract
                # (they would resolve against the process CWD). Surface
                # the caller bug instead of silently answering from it.
                log.error(
                    f"{self.machine_name}: relative sidecar probe {path!r} "
                    f"cannot use the scan listings; the storage root should "
                    f"already be joined. Falling back to os.path.isfile."
                )
        return os.path.isfile(path)

    def _createDictFromMetadataMapping(self, clip):
        metadata_dict = {}
        clip_metadatas = clip.metadatas

        for clip_metadata_key, clip_metadata_value in clip_metadatas.items():
            # Get metadata mappings
            metadatamappings = MetadataMapping.objects.filter(
                metadata_provider=clip_metadata_key
            )
            for metadatamapping in metadatamappings:
                metadata_dict[metadatamapping.metadata_portal] = clip_metadata_value

        return build_nested(metadata_dict)

    def mapMetadatas(self, clip_metadatas, values={}):
        for clip_metadata in clip_metadatas:
            # Get metadata mappings
            metadatamapping = MetadataMapping.objects.filter(
                metadata_provider=clip_metadata.name
            )
            if len(metadatamapping) > 0:
                log.debug(
                    "Field %s will have value: %s"
                    % (metadatamapping[0].metadata_portal, clip_metadata.value)
                )
                values[metadatamapping[0].metadata_portal] = clip_metadata.value
        return values

    def getSpannedClips(self, clip):
        return False

    def getAvailableMetadatas(self):
        return (
            ("clipname", "Clip name"),
            ("duration", "Clip duration"),
            ("timecode", "Timecode"),
            ("framerate", "Frame rate"),
            ("shooting_date", "Shooting date"),
            ("device_manufacturer", "Device manufacturer"),
            ("device_model", "Device Model"),
            ("device_serial", "Device Serial"),
            ("video_codec", "Codec Video"),
            ("aspect_ratio", "Aspect Ratio"),
            ("creation_date", "Creation date"),
            ("last_update_date", "Last update date"),
            ("user_clip_name", "User clip name"),
        )

    def getFileIdFromFullPath(self, path):
        _sh = StorageHelper()
        uri, storage_id = _sh.getStorageFromFullFileName(os.path.normpath(path))
        if not uri:
            log.warning("File uri for %s cannot be found" % path)
            return None
        try:
            file = _sh.getFileByPath(storage_id, path=uri)
            return file.getId()
        except NotFoundError:
            log.warning("File %s cannot be found" % uri)
            return None

    # PArse an XML file
    def parseXML(self, path):
        return XMLParser(path)

    def getImportOptions(self):
        return {}

    def getClipMainMediaFile(self, clip):
        """The clip's ANCHOR media file, as a dict, or ``None``.

        Keys the ingest reads: ``file_id``, ``path``, ``type`` and —
        optionally — ``MAIN_FILE_YIELDS_VIDEO``, which declares whether
        Vidispine will extract a video component of its own from this
        file. Omit it and it reads ``True`` (``yields_video_component``),
        which is the answer for every anchor Vidispine can decode.

        Overriding THIS hook is how a provider narrows the answer; see
        ``providers/red.py``, which derives it from REDline's ``Abs TC``.
        """
        return None

    def getClipMediaFiles(self, clip):
        media_file = self.getClipMainMediaFile(clip)
        if media_file:
            return [media_file]
        return None

    def getClipAdditionalMediaFiles(self, clip):
        return []

    def getAdditionalShapesToImport(self, clip):
        return None

    def getAvailableItem(self, clip):
        return None
