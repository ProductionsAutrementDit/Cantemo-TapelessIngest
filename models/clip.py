import logging
import re
import time
from collections.abc import Mapping
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

import os
import urllib
import pyxb.utils, simplejson as json
from django.urls import reverse
from django.db import models, transaction
from django.contrib.auth.models import User
from django.conf import settings
from django.core.cache import cache

from RestAPIBase.resturl import RestURL
from RestAPIBase.utility import perform_request, prepare_request, RestAPIBaseComError

from VidiRest.itemapi import ItemAPI
from VidiRest.objects.shape import VSShape
from VidiRest.helpers.vidispine import (
    createMetadataDocumentFromDict,
    createMergedBatchItemMetadataDocument,
)


from portal.api.v2.utils import format_datetime
from portal.vidispine import signals
from portal.vidispine.ijob import JobHelper
from portal.vidispine.iitem import ItemHelper
from portal.vidispine.icollection import CollectionHelper
from portal.vidispine.igroup import GroupHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.iuser import UserHelper
from portal.vidispine.iexception import handleRestAPIError, NotFoundError, VSAPIError
from portal.vidispine.igeneral import performVSAPICall
from portal.items.cache import invalidate_item_cache
from portal.utils.templatetags.vidispinetags import (
    getJobStatusLabel,
    getJobTypeLabel,
)
from portal.utils.templatetags.datetimeformatting import datetimeobject
from portal.plugins.TapelessIngest.helpers import (
    TapelessIngestHelper,
    TapelessIngestException,
)

from portal.plugins.TapelessIngest.models.settings import (
    Settings,
    MetadataMapping,
)
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.providers.providers import (
    main_file_verdict_is_unknown,
    yields_video_component,
)
from portal.plugins.TapelessIngest.scan.context import browse_root_path
from portal.plugins.TapelessIngest.scan.extraction import extract_metadatas
from portal.plugins.TapelessIngest.scan.ingestion import needs_hash_recovery
from portal.plugins.TapelessIngest.scan.persistence import (
    BULK_BATCH_SIZE,
    MetadataWrite,
    chunked,
    group_stale_deletes,
)

log = logging.getLogger(__name__)

# Alias over the canonical membership tuple. Registry order is load
# bearing: it decides which provider claims a file when several are
# applicable. See docs/adding-a-provider.md.
PROVIDERS_LIST = list(PROVIDER_NAMES)


def job_id_from_response(response: Any) -> Optional[str]:
    """The import job id in a Vidispine import response, or None.

    FR-36 turns "no job id" into a failure verdict, so the check must
    survive the shapes a helper can actually answer with: a mapping
    without ``jobId``, an empty body, ``None``, or a non-mapping. A bare
    ``"jobId" in response`` raises TypeError on the last two — out of
    ``import_file``, past the per-clip catch, and into the folder's
    error list instead of the failed counter.
    """
    if not isinstance(response, Mapping):
        return None
    return response.get("jobId") or None


# ---------------------------------------------------------------------------
# The wait for the extra components, and its two bounds
# ---------------------------------------------------------------------------
#
# `_import_multi_component` imports every EXTRA component first and the
# anchor last. It is the ANCHOR's job that evaluates the placeholder,
# creates the shape and starts the transcode — and nothing re-evaluates a
# placeholder afterwards. An anchor that reaches that step before the
# last extra has attached its file sees an incomplete set, logs
# "Skipping transcode", and the item keeps all its media on a shape that
# is never promoted, never tagged `original` and never transcoded.
# Measured on a 29-clip batch on 2026-08-31: the only 2 failures were the
# only 2 clips whose anchor job finished before the last segment job
# (16:37:31 vs 16:37:51; 16:58:50 vs 16:59:13). Codemill's own
# `importFileToPlaceholder` carries the same race.
#
# THE BOUND IS PER ENTRY POINT, because what a wait costs is not the same
# thing on both. The scan is a cron job that can afford to sit on a clip;
# `views.py` holds a DRF request thread and its database connection for
# the whole of it, so it gets a bound measured in "the caller is still
# there", not in "this job is never coming back".
#
# Neither is a normal cost: an extra component import attaches an
# ALREADY-HASHED file to a placeholder and settles in seconds. The 31-clip
# STARLUX batch measured 2 to 5 minutes per clip END TO END, transcode
# included, so five minutes is the outer edge of "wedged", not of "slow".
EXTRA_COMPONENT_WAIT_SECONDS = 300.0
REST_EXTRA_COMPONENT_WAIT_SECONDS = 30.0

# ... and the per-clip bound alone is not enough, because a request does
# not ingest ONE clip. `views.py`'s `__all__` branch runs a whole folder
# through `Folder.ingest`, so a 50-clip folder against a wedged
# Vidispine would hold that request thread and its database connection
# for 50 x 30 s. One BUDGET is therefore shared by all of a request's
# component waits, and every per-clip bound is clamped against what is
# left of it: the first clips may spend the full 30 s, the ones after
# them get whatever remains, and the ones past it do not wait at all.
#
# It is a COMPONENT-WAIT budget, not a request budget, and the name says
# so: it bounds the waiting this story introduced and nothing else. The
# scan pass, the metadata extraction, the collection resolution and every
# other Vidispine call the request makes are outside it — a request can
# still take longer than this, it just cannot spend longer than this
# WAITING for components. That distinction is also why the budget is
# passed as a DURATION and turned into a deadline at the start of the
# INGEST leg (`Folder._ingest_pass`): opening it before the scan pass
# would let a large healthy folder eat the whole budget in discovery and
# leave every clip a 0 s bound.
REST_COMPONENT_WAIT_BUDGET_SECONDS = 60.0

# The poll BACKS OFF. A wedged Vidispine is the case this loop exists for
# and it is exactly the case where hammering it every two seconds for five
# minutes makes things worse; the interval grows to the ceiling and the
# loop logs only when the pending set CHANGES, never once per iteration.
EXTRA_COMPONENT_POLL_SECONDS = 2.0
EXTRA_COMPONENT_POLL_BACKOFF = 1.5
EXTRA_COMPONENT_POLL_MAX_SECONDS = 15.0

# How many times the landing check is repeated once every component job
# has reached a TERMINAL status. A terminal job is not a landed one — it
# may have ended FAILED_TOTAL or ABORTED, attaching nothing — but neither
# is a single read of the shape, which can legitimately race a job that
# committed its attachment microseconds ago. Two reads, one FLOOR apart,
# separate "it landed" from "it stopped without landing" without waiting
# out the whole bound for a job that genuinely failed.
#
# The floor is its own constant and is deliberately NOT clamped to the
# remaining bound: once the deadline has passed the ordinary poll sleep
# clamps to 0, and two confirmations back to back absorb none of the
# attachment race they exist for. The overshoot is bounded by
# LANDING_CONFIRMATIONS x LANDING_CONFIRMATION_SECONDS.
LANDING_CONFIRMATIONS = 2
LANDING_CONFIRMATION_SECONDS = 1.0

# Vidispine's job statuses, read off `VSJob.STATUSES` on this server.
#
# `VSJob.inProgress()` is NOT "has it stopped": verified in the vendor
# bytecode on the 6.2.1 server, it answers True for STARTED, READY and
# STARTED_ASYNCHRONOUS and False for everything else — including
# **WAITING**, which is an ordinary status on a busy Vidispine and
# exactly the condition this wait exists for. Reading it as "stopped"
# emptied the pending set on the first pass, so the wait concluded
# "never attached although every import job has stopped" after two
# confirmations instead of using its bound, and the resume missed a
# WAITING job and re-imported its component.
#
# So the STATUS decides, and `inProgress()` is only the fallback for a
# job object that cannot give one.
JOB_STATUSES_STILL_COMING = frozenset(
    {"READY", "STARTED", "STARTED_ASYNCHRONOUS", "WAITING"}
)
JOB_STATUSES_TERMINAL = frozenset(
    # `FAILED` is not in `VSJob.STATUSES` (which spells it FAILED_TOTAL)
    # and is carried anyway: a status this set does not know is treated
    # as still coming, and "FAILED" is the one spelling where that
    # default would be actively wrong.
    {"FINISHED", "FINISHED_WARNING", "FAILED_TOTAL", "FAILED", "ABORTED"}
)

# The component types an import can actually attach. ONE definition, read
# by both the import loop and `_expected_file_ids`, because when they
# disagreed a provider that returns a non-media extra — `xdcam` really
# does build `{"type": "metadatas", ...}` with a `file_id` — put a file
# id into `expected` that can never attach: `missing` was non-empty for
# ever, the shape never reached the dead-end verdict, and every run
# re-entered the resume.
IMPORTABLE_COMPONENT_TYPES = frozenset({"audio", "video"})


def importable_extras(extra_files: Any) -> List[Dict[str, Any]]:
    """The extras an import can attach: the ones whose TYPE it can send.

    Filtered on the type ALONE. A media-typed extra that carries no
    ``file_id`` is deliberately still in here: it is a DEFECT to report,
    not a filter criterion. Excluding it made it budget nothing, expect
    nothing, import nothing and report nothing — and the clip came back
    INGESTED with a span file silently missing from the item, which is
    the same silence the whole story is about.

    A non-media type IS a legitimate skip: `xdcam` builds
    ``{"type": "metadatas", ..., "file_id": <real id>}`` for its sidecar
    XML, no import ever sends it, and `ignore_sidecars=True` keeps
    Vidispine from attaching it — so it can never be on the shape.
    """
    return [
        media_file
        for media_file in extra_files or ()
        if isinstance(media_file, Mapping)
        and media_file.get("type") in IMPORTABLE_COMPONENT_TYPES
    ]


def extras_without_a_file_id(extra_files: Any) -> List[Dict[str, Any]]:
    """Media-typed extras Vidispine has no file id for — a REPORTABLE gap."""
    return [
        media_file
        for media_file in importable_extras(extra_files)
        if not media_file.get("file_id")
    ]


# The Vidispine job type an import to a placeholder runs as. The RESUME
# path needs it: a run that starts while the previous run's component
# jobs are still IN FLIGHT must not re-import a component whose job is
# still running.
PLACEHOLDER_IMPORT_JOB_TYPE = "PLACEHOLDER_IMPORT"


# WHICH file a running job is importing is read off the job OBJECT, by
# PATH — never off the job's `data` list.
#
# MEASURED on the 6.2.1 server 2026-09-01, on two real PLACEHOLDER_IMPORT
# jobs of one multi-component RED clip (VX-696013, an extra component,
# and VX-696024, the anchor, both on item VX-216268): the whole of their
# `data` is `item` for the extra, and `errorMessage`, `item`,
# `transcodeProgress`, `transcodeWallTime` for the anchor. There is NO
# `sourceFileId` and NO `fileIds`. A first version of this code read
# those two keys — they appear in the vendor's own test fixture — and
# would therefore have answered "nothing is in flight" on this server for
# ever, silently degrading to the re-import it exists to prevent.
#
# What the job object DOES expose is the source, through the accessor
# family `get_related_jobs` already uses:
#
#   getSourceFilePath() -> 'file:///mnt/PAD_Storage/AA%20-%20RUSHES%20
#                           TAPELESS/2026/.../K001_K003_0804O6_002.R3D'
#   getFilename()       -> 'K001_K003_0804O6_002.R3D'
#   getTargetItem()     -> 'VX-216268'
#
# so a component is identified by matching that path against the
# provider's own `path` for each media file. The two sides are NOT
# normalised the same way, which is what the pair below is for: only the
# job side is a URI.


def _normalised_media_path(value: Optional[str]) -> Optional[str]:
    """A provider's own ``path`` as one comparable path string.

    NOT unquoted. A provider path comes from ``VSFile.getPath()`` — a
    bare filesystem path that was never percent-encoded — so unquoting it
    would REWRITE a real filename containing a ``%`` sequence
    (``100%25.R3D``, ``A%2FB.mov``) into something that is not the file,
    and the comparison would then match the wrong media or nothing at
    all.
    """
    if not value:
        return None
    path = str(value)
    if not path.strip():
        return None
    return os.path.normpath(path)


def _normalised_source_uri(value: Optional[str]) -> Optional[str]:
    """A job's ``getSourceFilePath()`` as one comparable path string.

    Parsed with ``urlsplit`` rather than by stripping a literal
    ``file://``: that covers ``file://host/path`` (where the host is not
    part of the path) and any other scheme this accessor might answer,
    where a prefix strip would silently fold the host into the path.

    Then UNQUOTED — this side really is a URI, the production storage
    root is ``/mnt/PAD_Storage/AA - RUSHES TAPELESS``, and a comparison
    against the raw value matches nothing and fails silently — and
    normalised.

    ``None`` for anything that leaves nothing to compare, which the
    caller must treat as "this job identifies no component", never as
    "it identifies none of mine".
    """
    if not value:
        return None
    raw = str(value)
    parts = urllib.parse.urlsplit(raw)
    # A bare POSIX path has no scheme; a Windows drive letter would parse
    # as a one-character scheme, which is why the guard is on length.
    if len(parts.scheme) > 1:
        path = urllib.parse.unquote(parts.path)
    else:
        path = raw
    if not path.strip():
        return None
    return os.path.normpath(path)


def _sleep(seconds):
    """The poll's only sleep, behind a module-level name.

    Tests patch THIS, never `time.sleep` — monkeypatching the attribute
    on the shared `time` module mutates it for the whole interpreter,
    which is a cross-test hazard rather than a seam.
    """
    time.sleep(seconds)


class PlaceholderShape(NamedTuple):
    """What `_get_or_create_placeholder_shape` resolved, and its state.

    ``shape_id`` is ``None`` when the item cannot be imported into at all
    — either the placeholder is COMPLETE and still a placeholder (a dead
    end no re-run can fix), or it holds a file that is not this clip's
    (not a clean resume; refused rather than imported into). Otherwise it
    names the shape to import into, and ``attached_file_ids`` says which
    of this clip's files are ALREADY on it, so a resumed import skips
    them instead of importing a file twice (or over-running the declared
    budget with the duplicate).

    ``created`` says whether this call MINTED the shape. A shape this run
    just created cannot have a previous run's import jobs pointing at it,
    which is what lets the resume path buy its job listing only for the
    items that can actually need it.
    """

    shape_id: Optional[str]
    attached_file_ids: FrozenSet[str] = frozenset()
    created: bool = False


# ---------------------------------------------------------------------------
# Segment grouping: the three clip-assembly rules
# ---------------------------------------------------------------------------
#
# A camera that splits one take across several files (`red`: `X_001.R3D`,
# `X_002.R3D` … `X_NNN.R3D`) hands discovery N files for ONE clip. Which
# of them anchors that clip is decided HERE, when clips are assembled —
# not in the discovery query, which returns them all.
#
# The rules, in this order:
#
#   1. `X_001.R3D` is present -> ONE clip anchored on it; `X_002…X_NNN`
#      are its extra files (attached at ingest by the provider's
#      `getClipAdditionalMediaFiles`) and are never probed as clip
#      candidates of their own.
#   2. A name carrying NO increment (`SOMECLIP.R3D`) -> its own clip, no
#      extras. Since 2026-08-28 this ALSO covers a name that merely ends
#      in three digits with nothing else of its stem beside it — a lone
#      `SHOT_042.R3D` is an ordinary filename, not a broken set, and must
#      be ingested rather than reported forever.
#   3. An increment with no `_001` anchor AND at least one OTHER increment
#      of the same stem beside it -> an ERROR, never a clip. The second
#      condition is what makes rule 3 evidence of a genuinely incomplete
#      multi-file set, which is what the 24 mis-anchored production clips
#      were (`…_004.R3D`, `…_012.R3D`, `…_026.R3D` standing in for their
#      clip's first segment).
#
# EXTRAS, NOT SPANNED CLIPS (ruled by Camille 2026-08-28, recorded here
# because it is domain knowledge the code cannot carry). `Clip.spanned`,
# `spanned_order`, `spanned_id`, `master_clip` and
# `Provider.getSpannedClips()` already exist and look like they should
# serve this — `USER_GUIDE.md`'s "Spanned Clips" section even uses
# `A001_C001_001.R3D`/`_002`/`_003` as its worked example. They are for a
# DIFFERENT shape: a take spread across SEVERAL PHYSICAL CARDS (P2,
# XDCAM), reunited as N linked `Clip` rows with one master. RED segments
# are the other shape — one take whose media is split into numbered files
# in ONE place — which is ONE row carrying N files on its item. The two
# are complementary, not competing; nothing here reads or writes a
# spanned field, and a take that is both segmented and card-spanned is
# out of scope (recorded as deferred work).
#
# None of this touches `Clip.umid` — the RED clip UUID read from the
# media, shared by every segment. Grouping changes which file ANCHORS a
# clip, never the clip's identity, so a clip already ingested stays
# already-ingested.

# `<stem>_<three digits>`, matched against the name with its extension
# already stripped. Three digits is the camera's own format
# ("{0:0=3d}"). Anchoring both ends is load bearing: `X_1000` has four
# digits after its only underscore and does NOT match (it is an ordinary
# name, not segment 1000 of anything), and `_001` has no stem and does
# not match either.
SEGMENT_INDEX_RE = re.compile(r"\A(?P<stem>.+)_(?P<index>[0-9]{3})\Z")

# The increment that anchors a clip. Not "the lowest increment present":
# a card whose `_001` was not copied must be reported, not silently
# re-anchored, which is the whole point of rule 3.
SEGMENT_ANCHOR_INDEX = "001"

# What `segment_role` answers. ANCHOR/STANDALONE/UNGROUPED all mean
# "extract this file"; they are kept apart so each rule can be pinned on
# its own.
SEGMENT_UNGROUPED = "ungrouped"  # no applicable provider groups this suffix
SEGMENT_STANDALONE = "standalone"  # rule 2
SEGMENT_ANCHOR = "anchor"  # rule 1, the file that becomes the clip
SEGMENT_EXTRA = "extra"  # rule 1, a sibling of an anchor: skipped
SEGMENT_ORPHAN = "orphan"  # rule 3, an incomplete set: reported, not a clip


def segment_stem(filename: str) -> Optional[Tuple[str, str, str]]:
    """``(stem, index, extension)`` for a segmented name, else ``None``.

    The one parser. ``red.getClipAdditionalMediaFiles`` reads it too, so
    the set of files the SCAN drops as extras and the set the INGEST
    re-attaches are derived from the same rule rather than from two
    independent guesses about the naming.
    """
    stem, extension = os.path.splitext(filename)
    match = SEGMENT_INDEX_RE.match(stem)
    if match is None:
        return None
    return match.group("stem"), match.group("index"), extension


def _validated_suffixes(provider: Any) -> Tuple[str, ...]:
    """One provider's ``getSegmentedExtensions()``, checked.

    Refuses LOUDLY rather than degrading. A bare string would be iterated
    character by character — a declaration of ``".R3D"`` would become the
    suffixes ``.``/``R``/``3``/``D`` and group every filename ending in
    ``d`` — and a suffix without its leading dot would do the same kind of
    damage more quietly. Both are provider bugs that must not be absorbed
    into a scan that silently stops ingesting media.
    """
    declared = getattr(provider, "getSegmentedExtensions", None)
    if not callable(declared):
        # A duck-typed double predating the hook groups nothing.
        return ()
    label = getattr(provider, "machine_name", None) or repr(provider)
    value = declared()
    if value is None:
        return ()
    if isinstance(value, str):
        raise TapelessIngestException(
            f"provider {label} returned the bare string {value!r} from "
            f"getSegmentedExtensions(); expected a sequence of suffixes — "
            f"iterating it would group every filename ending in one of its "
            f"characters"
        )
    suffixes = []
    for suffix in list(value):
        if not isinstance(suffix, str) or not suffix.startswith(".") or len(suffix) < 2:
            raise TapelessIngestException(
                f"provider {label} declared the unusable segmented extension "
                f"{suffix!r}; it must be a non-empty string beginning with '.'"
            )
        suffixes.append(suffix)
    return tuple(dict.fromkeys(suffixes))


def segmented_extensions_by_provider(
    provider_list: Optional[List[Any]],
) -> Dict[int, Tuple[str, ...]]:
    """``{id(provider): suffixes}`` for the providers that group.

    Keyed by IDENTITY, like ``ExtensionMap._ranks``: two instances of one
    class must be told apart. Built once per scan invocation and consulted
    per file, so a file is only ever grouped by the suffixes of a provider
    that is APPLICABLE to it — declaring ``.R3D`` does not let `red`
    suppress a file `red` would never be offered.
    """
    by_provider = {}
    for provider in provider_list or ():
        suffixes = _validated_suffixes(provider)
        if suffixes:
            by_provider[id(provider)] = suffixes
    return by_provider


def segmented_extensions(provider_list: Optional[List[Any]]) -> Tuple[str, ...]:
    """The flat union of every grouped suffix, order-stable and validated.

    The case is PRESERVED, and that is load bearing (see ``segment_role``).
    """
    suffixes: List[str] = []
    for provider in provider_list or ():
        suffixes.extend(_validated_suffixes(provider))
    return tuple(dict.fromkeys(suffixes))


def segment_suffixes_for(
    providers: Any, by_provider: Dict[int, Tuple[str, ...]]
) -> Tuple[str, ...]:
    """The grouped suffixes BINDING on one file.

    ``providers`` is that file's APPLICABLE set (the extraction
    pre-filter's answer), not the registry: a provider that declares a
    segmented suffix it does not also claim through ``getExtensions()``
    would otherwise suppress files it is never offered and never
    re-attaches, and the media would simply never be ingested. Within the
    applicable set a declaration is binding on everyone, because there is
    one clip per anchor rather than one per interested provider.
    """
    if not by_provider:
        return ()
    suffixes: Tuple[str, ...] = ()
    for provider in providers or ():
        suffixes += by_provider.get(id(provider), ())
    return suffixes


def _is_sibling_increment(name: str, stem: str, extension: str) -> bool:
    """Is ``name`` another numbered segment of ``stem``+``extension``?"""
    if not name.endswith(extension):
        return False
    parsed = segment_stem(name)
    return parsed is not None and parsed[0] == stem


def segment_role(
    filename: str,
    segmented_suffixes: Tuple[str, ...],
    siblings: Any,
) -> str:
    """Which of the assembly rules ``filename`` falls under.

    Args:
        filename: the BASENAME of a discovered file.
        segmented_suffixes: the suffixes the providers APPLICABLE TO THIS
            FILE group. Matched CASE-SENSITIVELY, because a provider must
            declare the case its own runtime guard accepts: `red` guards
            on ``file_extension == ".R3D"`` and declares ``".R3D"``, so a
            lowercase ``x_002.r3d`` — which `red` would decline and the
            `file` provider would claim as a clip of its own — is
            ``UNGROUPED`` and keeps being ingested exactly as before.
            Lowercasing here silently deleted such a card's media.
        siblings: called with no arguments, at most once, and only for a
            non-anchor increment. It returns the names in the file's
            directory — a FILESYSTEM question, not an index one, so a card
            whose segments span two result pages groups the same way one
            that fits on a single page does.

    Returns:
        ``UNGROUPED``/``STANDALONE``/``ANCHOR`` (extract this file),
        ``EXTRA`` (skip it, the anchor's clip owns it) or ``ORPHAN``
        (report it, never a clip).
    """
    if not any(filename.endswith(suffix) for suffix in segmented_suffixes or ()):
        return SEGMENT_UNGROUPED
    parsed = segment_stem(filename)
    if parsed is None:
        return SEGMENT_STANDALONE
    stem, index, extension = parsed
    if index == SEGMENT_ANCHOR_INDEX:
        return SEGMENT_ANCHOR
    names = siblings() or ()
    if f"{stem}_{SEGMENT_ANCHOR_INDEX}{extension}" in names:
        return SEGMENT_EXTRA
    for name in names:
        if name != filename and _is_sibling_increment(name, stem, extension):
            # Another increment of the same stem, and no `_001`: a set
            # that was copied incompletely.
            return SEGMENT_ORPHAN
    # Rule 2 (amended 2026-08-28): nothing else of this stem is here, so
    # the three digits are just how the file is named.
    return SEGMENT_STANDALONE


class ItemAPIEnhanced(ItemAPI):
    def getItemShapeIdsFromNames(
        self,
        item_id,
        shape_names,
        runasuser=None,
        return_format="json",
        placeholder=False,
    ):
        rest_url = RestURL("%sAPI/item/%s/shape" % (self.vsapi.super_url, item_id))
        query = {"tag": shape_names}
        if placeholder:
            query["placeholder"] = "true"
        rest_url.addQuery(query)
        param_dict = prepare_request(
            (self.vsapi.base64string),
            (rest_url.geturl()),
            runasuser=runasuser,
            return_format=return_format,
        )
        result = perform_request(**param_dict)
        if result:
            if return_format == "json":
                result = json.loads(result)
        return result


class ItemHelperExtended(ItemHelper):
    def provideItemAPI(self):
        if not hasattr(self, "itemapi"):
            self.itemapi = ItemAPIEnhanced(self._vsapi)

    def getItemShapesFromNames(self, item_id, shape_names, placeholder=False):
        """
        Get a list of item shapes, give a list of shape names

        Args:
            * item_id = The item ID
            * shape_names = A comma separated list of shape names

        Returns:
            * A list of VSShape objects representing the shapes
        """
        try:
            sr = self.itemapi.getItemShapeIdsFromNames(
                item_id, shape_names, runasuser=(self.runas), placeholder=placeholder
            )
            shapes = []
            for shape_id in sr.get("uri", []):
                res = self.itemapi.getItemShape(
                    item_id=item_id, shape_id=shape_id, runasuser=(self.runas)
                )
                shapes.append(VSShape(res, settings.VIDISPINE_REPLACE_URLS))

            return shapes
        except RestAPIBaseComError as e:
            handleRestAPIError(e)
        except Exception as e:
            log.error(("Couldn't get shape ids, reason: %s" % str(e)), exc_info=True)
            raise


class Reel(models.Model):
    umid = models.CharField(primary_key=True, max_length=100)
    created_on = models.DateTimeField(auto_now=True)
    folder_path = models.TextField()
    media_xml = models.TextField()


class Clip(models.Model):
    NOT_IMPORTED = "Not imported"
    WRAPPED = "Wrapped"
    REGISTERED = "Registered"
    PLACHOLDER_CREATED = "Placeholder created"
    IMPORTED = "Imported"
    STATUS_NOT_IMPORTED = 0
    STATUS_WRAPPED = 1
    STATUS_REGISTERED = 2
    STATUS_PLACHOLDER_CREATED = 3
    STATUS_IMPORTED = 4
    STATUS = {
        STATUS_NOT_IMPORTED: NOT_IMPORTED,
        STATUS_WRAPPED: WRAPPED,
        STATUS_REGISTERED: REGISTERED,
        STATUS_PLACHOLDER_CREATED: PLACHOLDER_CREATED,
        STATUS_IMPORTED: IMPORTED,
    }

    # Class-level provider cache for performance
    _PROVIDER_CACHE: Dict[str, Any] = {}

    umid = models.CharField(primary_key=True, max_length=100)
    created_on = models.DateTimeField(auto_now=True)
    imported_on = models.DateTimeField(null=True)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    folders = models.ManyToManyField("Folder", null=True)
    folder_path = models.TextField()
    path = models.TextField()
    storage_id = models.CharField(max_length=255, null=True)
    output_file = models.TextField(null=True)
    file_id = models.CharField(max_length=255, null=True)
    reference_file = models.CharField(max_length=255, null=False)
    status = models.IntegerField(blank=True, default=STATUS_NOT_IMPORTED)
    progress = models.CharField(max_length=255, null=True)
    spanned = models.BooleanField(blank=True, default=False)
    spanned_order = models.IntegerField(blank=True, default=0)
    spanned_id = models.CharField(max_length=100, null=True)
    master_clip = models.BooleanField(default=False)
    provider_name = models.CharField(max_length=100, db_column="provider")
    collection_id = models.TextField(null=True)
    item_id = models.CharField(max_length=10, null=True, blank=True)
    job_id = models.CharField(max_length=10, null=True, blank=True)
    clip_xml = models.TextField()
    reel = models.ForeignKey(Reel, null=True, on_delete=models.CASCADE)

    def __str__(self):
        if self.umid:
            return self.umid
        elif self.absolute_path:
            return self.absolute_path
        else:
            return "Unknown clip"

    def __init__(self, *args, **kwargs):
        if "metadatas" in kwargs:
            metadatas = kwargs.pop("metadatas")
            self._metadatas = metadatas
        super(Clip, self).__init__(*args, **kwargs)

    @classmethod
    def get_provider_by_name(
        cls, provider_name: str, clip: Optional["Clip"] = None
    ) -> Any:
        """Dynamically load and instantiate a provider by name with caching.

        Args:
            provider_name: Name of the provider module (e.g., 'red', 'xdcam', 'p2')
            clip: Optional clip instance to associate with the provider

        Returns:
            Provider instance for the specified camera format
        """
        # Check cache first
        if provider_name in cls._PROVIDER_CACHE:
            return cls._PROVIDER_CACHE[provider_name]

        # Load and instantiate provider
        moduleName = f"portal.plugins.TapelessIngest.providers.{provider_name}"
        className = "Provider"
        module = __import__(moduleName, {}, {}, className)
        Provider = getattr(module, className)()

        # setdefault, not a plain assignment (story 3.1): two worker
        # threads can both miss the check above and both instantiate, and
        # the old check-then-set handed each its own instance. Instance
        # identity is load-bearing — ExtensionMap ranks providers by
        # id(provider) — so whichever write lands first wins for BOTH
        # callers; the loser's instance is discarded, never handed out.
        return cls._PROVIDER_CACHE.setdefault(provider_name, Provider)

    @classmethod
    def _get_provider_list(cls, providers: Optional[List[str]] = None) -> List[Any]:
        """Get list of provider instances from provider names.

        Args:
            providers: Optional list of provider names. If None, uses all configured providers

        Returns:
            List of provider instances
        """
        filtered_providers = []
        if providers is None:
            providers = PROVIDERS_LIST
        for name in providers:
            Provider = cls.get_provider_by_name(name)
            filtered_providers.append(Provider)
        return filtered_providers

    @classmethod
    def get_or_new(
        cls, defaults: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Tuple["Clip", bool]:
        """Get existing clip or create a new one without saving to database.

        Args:
            defaults: Optional dictionary of default values for new clip
            **kwargs: Query parameters to find existing clip

        Returns:
            Tuple of (clip instance, is_new flag) where is_new is True if clip was created
        """
        try:
            return cls.objects.get(**kwargs), False
        except cls.DoesNotExist:
            defaults = defaults or {}
            params = {k: v for k, v in kwargs.items()}
            params.update(defaults)
            # Try to create an object using passed params.
            return cls(**params), True

    @classmethod
    def extract_file_metadatas(
        cls,
        file: Any,
        provider_list: Optional[List[Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        matched: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Pass 1 of the scan's two-pass lookup: everything before the DB.

        Providers and identity validation only, so a page's umids can be
        collected first and looked up in ONE query (AD-6) instead of one
        ``get`` per file. Hash recovery is NOT here: it needs the clip's
        ``item_id``, i.e. the lookup's answer (see ``recover_item_id``).

        Args:
            file: VSFile instance to extract metadata from
            provider_list: Optional list of provider instances. If None, uses all providers
            context: Optional context dictionary shared across provider calls; it is
                mutated in place by the providers, never replaced
            matched: Optional out-list. Every provider that CONTRIBUTED to
                this file is appended to it — the multi-provider contract
                (AD-7/FR-14) made observable, so a caller deciding what a
                clip consumed does not have to read it back off
                ``metadatas["provider"]``, which is last-writer-wins.

        Returns:
            The merged metadatas dict

        Raises:
            TapelessIngestException: If no UMID found in file
        """
        if provider_list is None:
            provider_list = cls._get_provider_list()
        if context is None:
            context = {}
        # `provider_list` is already pre-filtered to the applicable
        # providers by the caller; every one of them runs and merges.
        metadatas = extract_metadatas(file, provider_list, {}, context, matched=matched)
        if "umid" not in metadatas.keys():
            raise TapelessIngestException("No UMID found in file %s" % file.getPath())
        return metadatas

    def recover_item_id(
        self, file: Any, legacy_storages: Optional[List[str]] = None
    ) -> Optional[str]:
        """Find this clip's item on a legacy storage, by file hash — if useful.

        The gate is ``scan.ingestion.needs_hash_recovery``: a clip that
        already has an ``item_id``, a file Cantemo has not hashed yet, or
        a run with no legacy storages configured all cost ZERO HTTP calls
        (FR-8).

        A recovered id is assigned to the clip whether the row is new or
        pre-existing — recovery only fires when there is no ``item_id``,
        so it can never overwrite a known one — and the scan's write unit
        persists it under a fill-only-NULL guard
        (``PersistencePlan.recovered_item_ids``).

        Every step is defensive: a storage that errors, a response of an
        unexpected shape and an unreadable hash must all cost the clip
        nothing more than this recovery attempt. Losing the clip here
        would drop it from the write plan and leave no row at all.

        Returns:
            The recovered item id, or None when the gate closed or no
            legacy storage knew the hash.
        """
        try:
            hash = file.getHash()
        except Exception:
            log.error(
                f"Cannot read the hash of {self.umid}'s scanned file",
                exc_info=True,
            )
            return None
        if not needs_hash_recovery(self.item_id, hash, legacy_storages):
            return None
        sh = StorageHelper()
        for legacy_storage in legacy_storages:
            try:
                results = sh.storageapi.getFilesInStorage(
                    legacy_storage, query={"hash": [hash], "includeItem": "true"}
                )
                item_id = self._item_id_from_hash_hits(results)
            except Exception as e:
                log.error(
                    f"Error getting files with hash {hash} in storage {legacy_storage}: {e}"
                )
                continue
            if item_id:
                self.item_id = item_id
                log.info(
                    f"Recovered item {item_id} for {self.umid} from "
                    f"storage {legacy_storage} by hash {hash}"
                )
                return item_id
        return None

    @staticmethod
    def _item_id_from_hash_hits(results: Any) -> Optional[str]:
        """The item id inside a ``getFilesInStorage`` answer, or None.

        Every level is optional on purpose: a hit count without a ``file``
        list, a file without an ``item``, an item without an ``id`` — any
        of them used to raise KeyError/IndexError out of the scan's
        per-file wrapper and cost the clip its row.
        """
        if not isinstance(results, dict):
            return None
        if not results.get("hits"):
            return None
        files = results.get("file") or []
        if not files:
            return None
        items = (files[0] or {}).get("item") or []
        if not items:
            return None
        return (items[0] or {}).get("id") or None

    @classmethod
    def new_clip_defaults(
        cls, file: Any, metadatas: Dict[str, Any], item_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """The column values a NEVER-SEEN clip is created with.

        The one copy of the new-row shape: both the per-file
        ``get_or_new`` path and the scan's batched path build a new clip
        from exactly this dict, so the two can never drift. ``item_id``
        stays None here — recovery runs after the row is known
        (``recover_item_id``), which is what lets a pre-existing row
        receive a recovered id too.
        """
        return {
            "umid": metadatas["umid"],
            "path": os.path.dirname(file.getPath()),
            "storage_id": file.getStorage(),
            "spanned": False,
            "item_id": item_id,
        }

    def attach_file_metadatas(self, file: Any, metadatas: Dict[str, Any]) -> "Clip":
        """Pass 2 decoration: bind the scanned file and its metadatas.

        Instance state only: ``metadatas`` is memoized (the folder's
        write unit persists it, once, in batch) and
        ``provider_name``/``file_id``/``reference_file`` do not reach the
        DB during a scan for an EXISTING clip (``CLIP_UPDATE_FIELDS``).
        """
        self.provider_name = metadatas["provider"]
        self.metadatas = metadatas
        self.file = file
        self.reference_file = file.getId()
        return self

    @classmethod
    def get_clip_from_file(
        cls,
        file: Any,
        provider_list: Optional[List[Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        legacy_storages: Optional[List[str]] = None,
    ) -> Tuple["Clip", bool]:
        """Extract clip metadata from file and get or create clip instance.

        The per-file composition of the two passes, kept for callers
        outside the batched scan path (one lookup query per file).

        Args:
            file: VSFile instance to extract metadata from
            provider_list: Optional list of provider instances. If None, uses all providers
            context: Optional context dictionary shared across provider calls; it is
                mutated in place by the providers, never replaced
            legacy_storages: Optional list of legacy storage IDs to check for existing items

        Returns:
            Tuple of (clip instance, created flag)

        Raises:
            TapelessIngestException: If no UMID found in file
        """
        metadatas = cls.extract_file_metadatas(
            file,
            provider_list=provider_list,
            context=context,
        )
        # Lookup FIRST, recovery second (2.5): the clip's own item_id is
        # what decides whether a legacy-storage hash lookup is worth any
        # HTTP call at all.
        clip, created = cls.get_or_new(
            umid=metadatas["umid"],
            defaults=cls.new_clip_defaults(file, metadatas),
        )
        clip.attach_file_metadatas(file, metadatas)
        clip.recover_item_id(file, legacy_storages)

        return clip, created

    @classmethod
    def get_clip_from_item(self, item, provider=None):
        clip, created = self.get_or_new(
            item_id=item.getId(),
            defaults={
                "path": os.path.dirname(item.getPath()),
                "storage_id": item.getStorage(),
                "spanned": False,
            },
        )
        pass

    def get_storage_helper(self) -> Any:
        """Get or create StorageHelper instance for this clip.

        Returns:
            StorageHelper instance for accessing Vidispine storage API
        """
        if not hasattr(self, "_sth"):
            self._sth = StorageHelper(slug=self.storage_id)
        return self._sth

    def get_spanned_clips(self) -> List["Clip"]:
        """Get all related spanned clips if this clip is part of a spanned set.

        Returns:
            List of spanned clip instances, or empty list if not spanned
        """
        if self.spanned:
            return self.spanned_clips.all()
        else:
            return []

    @property
    def storage(self) -> Optional[Any]:
        """Get Vidispine storage object for this clip with Redis caching.

        Returns:
            Storage object or None if storage_id is not set or not found
        """
        if not hasattr(self, "_storage"):
            if self.storage_id is None:
                return None

            # Try to get from cache first
            cache_key = f"storage:{self.storage_id}"
            cached_storage = cache.get(cache_key)
            if cached_storage is not None:
                self._storage = cached_storage
                return self._storage

            # If not in cache, fetch from API
            _sth = self.get_storage_helper()
            try:
                self._storage = _sth.getStorage(self.storage_id)
                # Cache for 5 minutes
                cache.set(cache_key, self._storage, 300)
            except NotFoundError:
                return None
        return self._storage

    @storage.setter
    def storage(self, storage: Any) -> None:
        """Set storage object and update storage_id.

        Args:
            storage: Vidispine storage object
        """
        self._storage = storage
        self.storage_id = storage.getId()

    @property
    def root_path(self) -> Optional[str]:
        """Get root path from storage browse URI.

        Returns:
            Root path string or None if not available
        """
        if not hasattr(self, "_root_path"):
            # Canonical block (scan/context.py); no resolvable root leaves
            # _root_path unassigned -> AttributeError, exactly as before.
            # Deliberate unification: first browse method wins now; the old
            # inline loop let the last one win (no prod storage has two).
            root_path = browse_root_path(self.storage)
            if root_path is not None:
                self._root_path = root_path
        return self._root_path

    @property
    def absolute_path(self):
        """Get absolute filesystem path by joining root_path and relative path.

        Returns:
            Absolute path string or False if root_path not available
        """
        if self.root_path:
            return os.path.join(self.root_path, self.path)
        return False

    @property
    def ingest_base_path(self):
        ti_settings = Settings.objects.get(pk=1)
        return ti_settings.base_folder

    @property
    def file(self):
        if not hasattr(self, "_file"):
            if self.file_id is None:
                return None

            # Try to get from cache first
            cache_key = f"file:{self.file_id}"
            cached_file = cache.get(cache_key)
            if cached_file is not None:
                self._file = cached_file
                return self._file

            # If not in cache, fetch from API
            _sth = StorageHelper()
            try:
                self._file = _sth.getFileById(self.file_id)
                # Cache for 5 minutes
                cache.set(cache_key, self._file, 300)
            except NotFoundError:
                return None
        return self._file

    @file.setter
    def file(self, file):
        self._file = file
        self.file_id = file.getId()

    @property
    def cached_file_hash(self):
        """This clip's file hash, from the scan memo ONLY — never over HTTP.

        The ingest ladder's ``has_hash`` input (``scan.ingestion``): it
        reads ``_file``, the file the scan attached, and not the ``file``
        property, which issues a ``getFileById`` call when the memo is
        cold — one call per clip to decide the clip must not cost any.

        A hash that cannot be read is treated as absent: under NFR-1 that
        means "skip and retry next run", never ingest without the dedup
        key.
        """
        file = getattr(self, "_file", None)
        if file is None:
            return None
        try:
            return file.getHash()
        except Exception:
            log.error(
                f"Cannot read the hash of {self.umid}'s scanned file",
                exc_info=True,
            )
            return None

    @property
    def provider(self):
        if not hasattr(self, "_provider"):
            self.provider = self.__class__.get_provider_by_name(
                self.provider_name, clip=self
            )
            self._provider.MetadataMappingModel = MetadataMapping
        return self._provider

    @provider.setter
    def provider(self, Provider):
        self._provider = Provider
        self.provider_name = Provider.machine_name

    @property
    def xml(self):
        """Parsed clip XML: memo, then the stored column, then the file.

        FR-12 read order: a non-empty ``clip_xml`` is authoritative and
        is never re-parsed from the card. A corrected sidecar on disk is
        therefore ignored for the life of the row — declared in
        tests/fr4-waivers.md.
        """
        if hasattr(self, "_xml"):
            return self._xml
        if self.clip_xml:
            try:
                self._xml = XMLParser.from_string(self.clip_xml)
            except Exception:
                # A corrupt stored column must not take a clip down; fall
                # through to the file, exactly as if nothing were stored.
                log.error(
                    f"Cannot parse the stored clip_xml of {self.umid}", exc_info=True
                )
            else:
                return self._xml
        if os.path.splitext(self.reference_file)[1] not in [".xml", ".XML"]:
            return None
        xml_file = os.path.join(self.folder_path, self.reference_file)
        if not os.path.isfile(xml_file):
            return None
        self._xml = self.provider.parseXML(xml_file)
        return self._xml

    @xml.setter
    def xml(self, value):
        self._xml = value

    def load_clip_xml(self, metadatas: Optional[Dict[str, Any]] = None) -> bool:
        """FR-12: serialize the provider's sidecar into ``clip_xml`` ONCE.

        Called by the scan write unit's plan-prep, before the row is
        inserted.

        Fills an EMPTY column only — that guard is what keeps a re-scan
        from re-reading and re-parsing every card sidecar — and only from
        a ``clip_xml_file`` the providers already resolved; the sidecar is
        located WITHOUT touching Vidispine (the memoized root only), so a
        scan buys no extra HTTP call. A parse failure is swallowed and
        logged: an optional column must never cost a clip its ingest.

        Returns:
            True when the column was filled by this call.
        """
        if self.clip_xml:
            return False
        if metadatas is None:
            metadatas = getattr(self, "_metadatas", None) or {}
        sidecar = metadatas.get("clip_xml_file")
        if not sidecar:
            return False
        xml_file = self._sidecar_absolute_path(sidecar)
        if xml_file is None:
            return False
        try:
            parsed = self.provider.parseXML(xml_file)
            serialized = parsed.tostring()
        except Exception:
            log.error(
                f"Cannot parse the clip XML {xml_file} of {self.umid}", exc_info=True
            )
            return False
        if isinstance(serialized, bytes):
            # lxml serializes to bytes; the column is text. Decoding here
            # is what makes the stored value re-parseable by `xml` above.
            serialized = serialized.decode("utf-8", "replace")
        self.clip_xml = serialized
        self._xml = parsed
        return True

    def _sidecar_absolute_path(self, sidecar: str) -> Optional[str]:
        """Absolute path of a provider-declared ``clip_xml_file``.

        Providers declare it either absolute (panasonicP2, ikegami) or
        relative to the clip's own directory (xdcam's ``./Clip/...``
        form, and the MEDIAPRO URIs) — the same basename-against-the-clip
        resolution ``xdcam.getClipFiles`` uses. Resolution reads the
        MEMOIZED root only, so it costs no storage lookup and cannot
        raise out of the ``root_path`` chain.
        """
        if os.path.isabs(sidecar):
            return sidecar
        root_path = getattr(self, "_root_path", None)
        if not root_path:
            return None
        return os.path.join(root_path, self.path, os.path.basename(sidecar))

    @property
    def metadatas(self):
        if not hasattr(self, "_metadatas"):
            self._metadatas = {}
            # First we try to get the metadatas from db
            for metadata in self.clipmetadata_set.all():
                self._metadatas[metadata.name] = metadata.value
        return self._metadatas

    @metadatas.setter
    def metadatas(self, new_metadatas):
        # Memo only (AD-6): persisting is the batched write unit's job —
        # models/folder.persist_scan_results for the scan path,
        # persist_metadatas() for everyone else.
        self._metadatas = new_metadatas

    @property
    def media_files(self):
        if not hasattr(self, "_media_files"):
            self._media_files = self.provider.getClipMediaFiles(self)
        return self._media_files

    @property
    def spanned_clips(self):
        if not hasattr(self, "_spanned_clips"):
            self._spanned_clips = self.provider.getSpannedClips(self)
        return self._spanned_clips

    @property
    def item(self):
        if not hasattr(self, "_item"):
            # Falsiness, not `is None`: the item-deletion listener writes
            # `""`, which must answer None here rather than reach
            # `getItem("")`.
            if not self.item_id:
                return None

            # Try to get from cache first
            cache_key = f"item:{self.item_id}"
            cached_item = cache.get(cache_key)
            if cached_item is not None:
                self._item = cached_item
                return self._item

            # If not in cache, fetch from API
            _ith = ItemHelper()
            try:
                self._item = _ith.getItem(self.item_id)
                # Cache for 3 minutes
                cache.set(cache_key, self._item, 180)
            except NotFoundError:
                return None
        return self._item

    @item.setter
    def item(self, item):
        if item:
            self._item = item
            self.item_id = item.getId()

    @property
    def job(self):
        if not hasattr(self, "_job"):
            # Falsiness, not `is None`: the item-deletion listener writes
            # `""`, which must answer None here rather than reach
            # `getJob("")` — the same rule as `Clip.item`.
            if not self.job_id:
                return None
            _ijh = JobHelper()
            try:
                self._job = _ijh.getJob(self.job_id)
            except NotFoundError:
                return None
        return self._job

    @job.setter
    def job(self, job):
        self._job = job
        self.job_id = job.getId()

    @property
    def collections(self):
        if not hasattr(self, "_collections"):
            if self.collection_id is None:
                return None
            ch = CollectionHelper()
            try:
                collections = self.collection_id.split(",")
                for collection in collections:
                    self._collections.append(ch.getCollection(self.collection_id))
            except NotFoundError:
                return None
        return self._collections

    def get_thumbnail_url(self):
        return reverse("clip_thumbnail", None, [str(self.umid)])

    def get_state(self):
        if hasattr(self, "jobs"):
            if self.jobs.status == "Ingest finished":
                return "IMPORTED"
            elif self.jobs.status == "Ingest failed":
                return "FAILED"
            else:
                return "PROCESSING"
        else:
            if self.status is self.STATUS_IMPORTED:
                return "IMPORTED"
            else:
                return "NOTIMPORTED"

    @property
    def error(self):
        return getattr(self, "_error", "")

    @error.setter
    def error(self, value):
        self._error = value

    def get_readable_duration(self):
        from portal.plugins.TapelessIngest.templatetags.tapelessingest_extras import (
            frame_to_time,
        )

        total_duration = 0
        if self.spanned and self.master_clip:
            clips_ids = []
            spanned_clips = self.spanned_clips.all()
            for spanned_clip in spanned_clips:
                clips_ids.append(spanned_clip.clip.umid)
            durations = ClipMetadata.objects.filter(clip__in=clips_ids, name="duration")
            for duration in durations:
                total_duration += int(float(duration.value))
        else:
            if "duration" in self.metadatas and self.metadatas["duration"] is not None:
                total_duration = int(float(self.metadatas["duration"]))
        return frame_to_time(total_duration)

    def get_readable_status(self):
        return self.STATUS[self.status]

    def get_absolute_url(self):
        return ""

    def get_resource_uri(self):
        return ""

    def get_related_jobs(self):
        if self.item_id:
            # check if there is vidispine jobs related
            jh = JobHelper()
            _jobs = jh.getAllJobsForItem(self.item_id)
            _pretty_jobs = []
            for _j in _jobs:
                joblink = reverse("vs_job", kwargs={"slug": _j.getId()})
                _pretty_jobs.append(
                    {
                        "id": _j.getId(),
                        "type": getJobTypeLabel(None, _j.getType()),
                        "state": getJobStatusLabel(None, _j.getStatus()),
                        "rawstatus": _j.getStatus(),
                        "user": _j.getUser(),
                        "startTime": format_datetime(
                            datetimeobject(_j.getStarted())
                        ).replace(" ", "&nbsp"),
                        "targetitem": _j.getTargetItem(),
                        "joblink": joblink,
                        "in_progress": _j.inProgress(),
                        "priority": _j.getPriority(),
                        "filename": _j.getFilename(),
                        "transcodeProgress": _j.getTranscodeProgress(),
                        "sourceFilePath": _j.getSourceFilePath(),
                    }
                )

            return self.json_response(
                {
                    "jobs": _pretty_jobs,
                },
                200,
            )
        else:
            return False

    def __unicode__(self):
        return "%s" % self.umid

    def create_item(
        self,
        user: Optional[User] = None,
        collection_id: Optional[str] = None,
        replace: bool = False,
        metadatagroupname: str = "Film",
        gh: Any = None,
        uh: Any = None,
        ith: Any = None,
        ch: Any = None,
    ) -> Tuple[Any, bool]:
        """Create a new Vidispine item with metadata or update existing item.

        Args:
            user: User performing the operation
            collection_id: Optional collection ID to add the item to
            replace: Whether to replace existing metadata if item exists
            metadatagroupname: Name of the metadata group to use
            gh: GroupHelper for user group operations. ``None`` (the
                default) means "build one per call, as ``user``".
            uh: UserHelper for user settings. ``None`` (the default)
                means "build one per call, as ``user``".
            ith: ItemHelper for item operations. ``None`` (the default)
                means "build one per call, as ``user``". A caller with a
                subclass (``import_file`` passes an
                ``ItemHelperExtended``) passes it explicitly and it is
                used as given.
            ch: CollectionHelper for collection operations. ``None`` (the
                default) means "build one per call, as ``user``".

        Returns:
            Tuple of (item object, created flag) where created is True if new item was created
        """
        created = False

        if self.item_id and self.item is None:
            # The row NAMES an item Vidispine cannot resolve — `Clip.item`
            # answers None on NotFoundError too (the accident commit
            # 632bd4c closed), so falling through would take the create
            # branch and make a SECOND placeholder beside the one the row
            # already names. Refuse instead, BEFORE the preparatory round
            # trips (a wedged row recurs every run by design, and should
            # cost one getItem, not the full ingest-group/settings/metadata
            # preamble): the per-clip handler counts this clip failed with
            # the reason below, every run, until the row is healed
            # (operator tooling is the maintenance story's).
            raise TapelessIngestException(
                f"clip {self.umid} names item {self.item_id}, which "
                f"Vidispine cannot resolve — refusing to create a "
                f"second placeholder for it; heal or reset the row"
            )

        # BELOW the wedged-row refusal above, not before it (retro-3
        # review): story 3.0's frozen intent is that a wedged row costs
        # ONE getItem, not the full preamble, and four helper
        # constructors ahead of the refusal quietly broke that — it
        # recurs every run by design, so its cost is a per-run cost.
        #
        # Story 3.1 review: `uh` defaults to None and is built PER CALL,
        # as the run's user. The old class-definition-time
        # ``UserHelper()`` default was one shared instance — with
        # ``runas=None`` — for every caller in the process; a future
        # caller omitting ``uh`` must not silently inherit it back.
        if uh is None:
            uh = UserHelper(runas=user)
        # Retro-3 F4: `gh`/`ith`/`ch` were the SAME defect class — one
        # shared class-definition-time instance each, with ``runas=None``,
        # for every caller in the process (cross-thread shared state under
        # the pool). Per call, as the run's user, exactly like ``uh`` and
        # like ``import_file`` already builds the ones it passes in. An
        # explicitly passed helper is used AS GIVEN, never rebuilt.
        if gh is None:
            gh = GroupHelper(runas=user)
        if ith is None:
            ith = ItemHelper(runas=user)
        if ch is None:
            ch = CollectionHelper(runas=user)

        ingestgroups, default_ingest_group = gh.getUserIngestGroups()
        ingestgroupname = default_ingest_group.name

        log.debug("found %s clip_metadatas associated" % len(self.metadatas))

        _metadata = self.provider._createDictFromMetadataMapping(self)

        settingsprofile_id = uh.getUserSettingsProfile(basegroup=ingestgroupname)

        md = createMetadataDocumentFromDict(_metadata, [metadatagroupname])

        if self.item is None:
            # If item doesn't exist, we create it
            created = True
            log.info("Creating placeholder...")
            self.item = ith.createPlaceholder(md, settingsprofile_id=settingsprofile_id)
            if not self.item_id:
                # The `item` setter silently no-ops on a falsy return, so
                # without this guard the combined write below would stamp
                # PLACEHOLDER_CREATED beside an EMPTY item_id — fabricating
                # the one cell the ladder refuses to act on — and execution
                # would carry on into setItemMetadataFieldGroup(None).
                raise TapelessIngestException(
                    f"createPlaceholder returned no item for {self.umid}"
                )
            # NFR-1 (story 3.0): make the placeholder durable NOW — one
            # targeted UPDATE writing `item_id` and `status` TOGETHER,
            # before any further Vidispine call. A death anywhere after
            # this statement leaves exactly the FR-36 incomplete-import
            # cell (item_id, no job_id, PLACEHOLDER_CREATED), which the
            # existing retry rung recovers into this same placeholder next
            # run. By pk, and never a save() fallback, which would INSERT
            # a partial row before the import ran. The OTHER writer of
            # these columns is `persist_ingest_state` (INGEST_STATE_FIELDS),
            # which runs after `import_file` returns and re-writes both
            # along with the job id. Deliberate side effect of `.update()`:
            # it bypasses `created_on`'s `auto_now` (a misnamed
            # last-modified column), so the FR-36 cell keeps its scan-time
            # timestamp.
            self.status = self.STATUS_PLACHOLDER_CREATED
            updated = (
                type(self)
                .objects.filter(pk=self.pk)
                .update(item_id=self.item_id, status=self.status)
            )
            if not updated and not self._state.adding:
                # Two different absences: an UNSAVED clip (the REST path)
                # legitimately has no row and stays a silent no-op, but a
                # PERSISTED clip whose row vanished mid-run means the
                # placeholder's name was just lost — a death before
                # persist_ingest_state re-inserts it would orphan it.
                log.error(
                    f"clip {self.umid}: the combined item_id/status UPDATE "
                    f"matched no row — a persisted clip's row was deleted "
                    f"concurrently, so placeholder {self.item_id} is not "
                    f"durably recorded until persist_ingest_state re-inserts "
                    f"the row"
                )
        else:
            # If item already exists, we check if we have to replace it
            if replace:
                # If we have to replace it, we merge existing metadatas with new ones
                custom_metadata = self.item.getMetadata()[0]
                md_to_set = createMergedBatchItemMetadataDocument(
                    md, custom_metadata, "replace"
                )
                ith.setItemMetadata(self.item_id, metadata_document=md_to_set)
                log.info("Item already exists, replacing new metadatas")
            else:
                # If we don't have to replace it, we simply return existing item
                return self.item, created

        ith.setItemMetadataFieldGroup(self.item_id, metadatagroupname)

        log.info("Placeholder creation done (id=%s)" % self.item_id)

        if collection_id:
            log.info(
                f"Found collection {collection_id} from path {self.path}, adding item {self.item_id} to it"
            )
            ch.addItemToCollection(collection_id, self.item_id)
        return self.item, created

    def _should_replace_original_files(
        self,
        original_files: List[Any],
        replace: bool,
        legacy_storages: Optional[List[str]],
    ) -> bool:
        """Check if original files should be replaced based on criteria.

        Args:
            original_files: List of existing original files for the item
            replace: Whether to replace existing files
            legacy_storages: List of legacy storage IDs to check against

        Returns:
            True if files should be replaced, False otherwise
        """
        if not self.file:
            return False

        # FR-35: this length test used to be `>=`, i.e. always true, so
        # the rung swallowed every non-replace call whatever the shape
        # held. An item whose original shape has NO file is not "already
        # imported" — it falls through to the checks below, which are the
        # ones that can actually tell.
        if len(original_files) > 0 and replace == False:
            log.info(
                f"Importing {self.item_id}: Item already exists and has an original file, skipping it"
            )
            return False

        if self.file.getId() in [f.getId() for f in original_files]:
            log.info(
                f"Importing {self.item_id}: File is already in original files, skipping it"
            )
            return False

        if self.file.getStorage() in [f.getStorage() for f in original_files]:
            log.info(f"Importing {self.item_id}: File is on same storage, skipping it")
            return False

        if legacy_storages:
            if any(f.getStorage() not in legacy_storages for f in original_files):
                log.info(
                    f"Importing {self.item_id}: File is not on legacy storage, skipping it"
                )
                return False

        return True

    def _remove_original_shapes(
        self, user: Optional[User], item_helper: Any, storage_helper: Any
    ) -> None:
        """Remove existing original shapes before replacement.

        Args:
            user: User performing the operation
            item_helper: ItemHelperExtended instance
            storage_helper: StorageHelper instance
        """
        original_shapes = item_helper.getItemShapesFromNames(self.item_id, ["original"])

        for original_shape in original_shapes:
            original_files = original_shape.getAllFiles()
            for _file in original_files:
                log.info(
                    f"Importing {self.item_id}: Removing file {_file.getId()} from original shape"
                )
                storage_helper.removeFileItemRelationship(
                    _file.getStorage(), _file.getId()
                )

            log.info(
                f"Importing {self.item_id}: Removing original shape {original_shape.getId()}"
            )
            item_helper.itemapi.removeItemShape(
                self.item_id, original_shape.getId(), runasuser=user
            )

    def _get_or_create_placeholder_shape(
        self,
        user: Optional[User],
        item_helper: Any,
        main_file: Optional[Dict[str, Any]] = None,
        extra_files: Optional[List[Dict[str, Any]]] = None,
    ) -> PlaceholderShape:
        """Resolve the placeholder shape to import into, and say which.

        THREE states, and they are not the same thing:

        * no placeholder shape — create one (a fresh item, or one whose
          real ``original`` shape a replace just removed);
        * a placeholder holding NO file — reuse it. That is what makes
          the FR-36 incomplete-import retry land on the same placeholder
          instead of minting a second one, and it stays silent;
        * a placeholder ALREADY HOLDING FILES — a previous import that
          got some way in and stopped. This used to be one undifferentiated
          ``log.info("Shape is not a placeholder")`` + ``return None``,
          which ``import_file`` turned into a bare ``failed``: the scan
          reported "31 failed, 0 errors" and every retry reproduced the
          same silent exit. It is now split in two:

          - INCOMPLETE (some of this clip's files are missing from the
            shape) — RESUMABLE. Nothing is wrong with the shape; a
            previous run simply did not get to the end. The missing
            components are imported, then the anchor, and the item
            promotes. A transient Vidispine slowdown must not turn into
            permanent manual work.
          - COMPLETE (every one of this clip's files is attached and the
            shape is STILL a placeholder) — a dead end. Vidispine was
            told to expect a component that nothing will ever fill, and
            no re-run can change that, so the operator is told what is
            attached, through the ERROR channel.

          A placeholder holding a file that is NOT this clip's
          (``attached - expected``) is neither: it is not a resume at
          all. Importing into it would add this clip's media to some
          other clip's item, so it is refused and reported.

        A REAL (non-placeholder) ``original`` shape holding files never
        reaches this method: ``import_file`` decides it at the FR-35 rung
        above, and that skip is untouched.

        Args:
            user: User performing the operation
            item_helper: ItemHelperExtended instance
            main_file: This clip's anchor media file, if known
            extra_files: This clip's extra media files, if known

        Returns:
            A ``PlaceholderShape``; its ``shape_id`` is ``None`` when the
            item is a dead end.
        """
        original_shapes = item_helper.getItemShapesFromNames(
            self.item_id, ["original"], placeholder=True
        )

        if original_shapes is None or len(original_shapes) == 0:
            log.info(f"Importing {self.item_id}: No original shape found, creating one")
            response = item_helper.itemapi.createPlaceholderShape(
                self.item_id, runasuser=user
            )
            return PlaceholderShape(response.decode("UTF-8"), created=True)

        shape = original_shapes[0]
        attached = frozenset(_file.getId() for _file in shape.getAllFiles())
        if not attached:
            return PlaceholderShape(shape.getId())

        expected = self._expected_file_ids(main_file, extra_files)
        anchor_id = main_file.get("file_id") if isinstance(main_file, Mapping) else None
        if anchor_id and anchor_id in attached:
            # THE ANCHOR HAS ALREADY LANDED and the shape is STILL a
            # placeholder. Its import job is the only thing that ever
            # evaluates the placeholder, and nothing re-evaluates one
            # afterwards — so whatever is or is not attached beside it,
            # no re-run can promote this item. This is a dead end even
            # when extras are missing, and saying so here is what stops
            # the resume re-importing the anchor: a second container
            # import lands a second component and earns the measured
            # 400.
            missing_here = sorted(expected - attached)
            message = (
                f"Importing {self.item_id}: placeholder shape {shape.getId()} "
                f"already holds this clip's anchor file {anchor_id} and is "
                f"STILL a placeholder"
                + (
                    f", with {len(missing_here)} of its component(s) never "
                    f"attached ({', '.join(missing_here)})"
                    if missing_here
                    else ""
                )
                + f"{self._component_budget_report(main_file, extra_files)}. The "
                f"anchor's import job is the only thing that evaluates a "
                f"placeholder and nothing re-evaluates one afterwards, so no "
                f"re-run can promote this item and re-importing the anchor "
                f"would only duplicate a component: the shape has to be "
                f"removed by hand in the Vidispine admin before the clip can "
                f"be ingested again"
            )
            log.error(message)
            self.error = message
            return PlaceholderShape(None)

        foreign = sorted(attached - expected)
        if foreign and self._expectation_is_complete(main_file, extra_files):
            # NOT a resume. Whatever this shape is holding, it is not
            # this clip's media, so importing into it would attach this
            # clip's files to another clip's item — and the `expected`
            # guard is what keeps that verdict off a caller that simply
            # did not say which files it wanted (`expected` empty), which
            # falls through to the dead-end rung below exactly as before.
            message = (
                f"Importing {self.item_id}: placeholder shape {shape.getId()} "
                f"holds {len(foreign)} file(s) that are not this clip's "
                f"({', '.join(foreign)}), so this is not a resume of this "
                f"clip's import — nothing is imported into it. Check which "
                f"item {', '.join(foreign)} belong(s) to before re-running: "
                f"either this clip resolved onto the wrong item, or the shape "
                f"has to be cleared by hand in the Vidispine admin"
            )
            log.error(message)
            self.error = message
            return PlaceholderShape(None)

        missing = sorted(expected - attached)
        if missing:
            log.info(
                f"Importing {self.item_id}: resuming placeholder shape "
                f"{shape.getId()}, which a previous import left holding "
                f"{len(attached)} of this clip's file(s); "
                f"{len(missing)} still to import ({', '.join(missing)})"
            )
            return PlaceholderShape(shape.getId(), attached)

        message = (
            f"Importing {self.item_id}: placeholder shape {shape.getId()} already "
            f"holds every file of this clip "
            f"({', '.join(sorted(attached))}) and is STILL a placeholder, so "
            f"Vidispine is waiting on a component slot nothing will ever fill"
            f"{self._component_budget_report(main_file, extra_files)}. Nothing "
            f"re-evaluates a placeholder once its anchor job has run, so no "
            f"re-run can promote this item: the shape has to be removed by hand "
            f"in the Vidispine admin before the clip can be ingested again"
        )
        log.error(message)
        self.error = message
        return PlaceholderShape(None)

    @staticmethod
    def _expected_file_ids(
        main_file: Optional[Dict[str, Any]],
        extra_files: Optional[List[Dict[str, Any]]],
    ) -> FrozenSet[str]:
        """Every Vidispine file id this clip's shape should end up holding.

        The extras are filtered through ``importable_extras`` — the SAME
        filter the import loop uses. When the two disagreed, a provider
        returning a non-media extra (``xdcam``'s
        ``{"type": "metadatas", ...}``, which carries a real ``file_id``)
        put a file id in here that no import would ever attach: ``missing``
        stayed non-empty for ever, so the shape never reached the
        dead-end verdict and every run re-entered the resume.

        Empty when the caller supplied no media files — the resume test
        then finds nothing missing and a non-empty placeholder is treated
        as the dead end it was before this story, which is the safe
        direction for a caller that cannot say what it wanted.
        """
        ids = {
            media_file["file_id"]
            for media_file in importable_extras(extra_files)
            if media_file.get("file_id")
        }
        if main_file and main_file.get("file_id"):
            ids.add(main_file["file_id"])
        return frozenset(ids)

    @staticmethod
    def _expectation_is_complete(
        main_file: Optional[Dict[str, Any]],
        extra_files: Optional[List[Dict[str, Any]]],
    ) -> bool:
        """Can the caller name EVERY file this shape should hold?

        Only then may a file on the shape be called FOREIGN. An anchor
        with no ``file_id`` — reachable on the REST path, where a clip is
        built from a request body — would otherwise make the item's own
        anchor file look like another clip's.
        """
        # The ANCHOR's file id is the whole question. A non-importable
        # extra (`xdcam`'s `metadatas` dict) is not a gap in the
        # expectation: no import ever sends it, `ignore_sidecars=True`
        # keeps Vidispine from attaching it, so it can never appear on
        # the shape and can never be mistaken for a foreign file.
        return bool(main_file and main_file.get("file_id"))

    def _component_budget_report(
        self,
        main_file: Optional[Dict[str, Any]],
        extra_files: Optional[List[Dict[str, Any]]],
    ) -> str:
        """The component slots this clip needs, named, for the error above.

        Empty when the caller did not supply the media files — the
        message stays truthful, it just says less.
        """
        if not main_file:
            return ""
        audio_count, video_count = self._count_media_components(
            main_file, extra_files or []
        )
        if main_file_verdict_is_unknown(main_file):
            # `_count_media_components` folds None into True for the
            # count; a diagnostic must not present that fold as a fact.
            return (
                f", where the component set it was told to expect is "
                f"container=1, video={video_count or 0}, audio={audio_count or 0} "
                f"— counting the anchor's own video component, which the "
                f"provider could NOT vouch for"
            )
        return (
            f", where the component set it was told to expect is container=1, "
            f"video={video_count or 0}, audio={audio_count or 0}"
        )

    def _record_job(self, job_id: str, job_helper: Any) -> None:
        """Record the import job this run rides on — the ID first.

        The ``job`` SETTER does ``self.job_id = job.getId()``. The GETTER
        catches ``NotFoundError``; the setter never did, so a ``getJob``
        answering None — or raising for a job Vidispine has already
        purged — aborted the clip's ingest with an ``AttributeError`` on
        the SUCCESS path of the resume: the branch that has just
        correctly decided NOT to start a second import.

        The id is written WHATEVER happens, before the fetch is even
        attempted. It is the only part of the job the row keeps
        (``INGEST_STATE_FIELDS`` persists ``job_id``, not the object), and
        swallowing it would leave the row job-less — which is exactly
        what the ``retry_incomplete`` rung reads as "never imported" and
        brings straight back here to fire the second import this branch
        just refused to send.

        Both entry points call this on EVERY import, fresh or resumed:
        the fresh sites did ``self.job = job_helper.getJob(job_id)`` raw,
        which is worse than the resume case — the import has already
        been SENT, so an ``AttributeError`` there left a row with no
        ``job_id`` while a real container import ran, and
        ``retry_incomplete`` fired a duplicate on the next run.

        The cache is written on every arm, ``None`` on the failing ones.
        The ``job`` getter is ``hasattr(self, "_job")``-gated and refetches
        through a FRESH ``JobHelper`` catching only ``NotFoundError`` —
        so a swallowed ``RuntimeError`` here re-raised on the next
        ``clip.job`` read (``ClipSerializer.job``, on the REST path), and
        a second ``_record_job`` on the same instance (a clip object CAN
        be imported twice in one process) kept the FIRST call's object
        under the second call's id. And the id is kept as sent, never
        re-read off the object: the setter's ``self.job_id = job.getId()``
        would silently replace it if the helper normalised ids.
        """
        self.job_id = job_id
        self._job = None
        try:
            job = job_helper.getJob(job_id)
        except NotFoundError as error:
            # Its own arm, with its own wording: Vidispine SAID the job
            # is gone, which is a different fact from a read that failed.
            log.warning(
                f"Importing {self.item_id}: job {job_id} is not known to "
                f"Vidispine (purged? {error}) — the job id is recorded on "
                f"the clip anyway"
            )
            return
        except Exception as error:  # noqa: BLE001 - an unreadable job is "no job"
            log.warning(
                f"Importing {self.item_id}: job {job_id} could not be read "
                f"({error}) — the job id is recorded on the clip anyway"
            )
            return
        if job is None:
            log.warning(
                f"Importing {self.item_id}: job {job_id} is not known to "
                f"Vidispine (purged? getJob answered nothing) — the job id is "
                f"recorded on the clip anyway"
            )
            return
        self._job = job

    def _import_single_component(
        self,
        main_file_id: str,
        user_groups: List[str],
        no_transcode: Optional[bool],
        ingest_helper: Any,
        job_helper: Any,
        in_flight_jobs: Any = (),
    ) -> bool:
        """Import a single-component (no extra files) clip.

        Args:
            main_file_id: File ID of the main media file
            user_groups: List of user groups for ingest profile
            no_transcode: Whether to skip transcoding
            ingest_helper: TapelessIngestHelper instance
            job_helper: JobHelper instance
            in_flight_jobs: Import jobs already running for this file
                (``Clip._in_flight_component_files``). A single-component
                clip has no component wait, but it has the same
                duplicate-import hazard: an interrupted run leaves the
                placeholder EMPTY while its job runs, so the shape is
                reused silently and the retry rung fires a second
                container import into it.

        Returns:
            True if import was successful, False otherwise
        """
        if not main_file_id:
            # The same refusal `_import_multi_component` makes for its
            # anchor. `getFileIdFromFullPath` answers None for a file
            # Vidispine does not know and `file.getClipMainMediaFile`
            # builds `{"file_id": None}` for a clip with no `file`, so
            # this is reachable — and sending `{"fileId": None}` would
            # either 400 or, worse, import something else.
            message = (
                f"Importing {self.item_id}: the anchor has no Vidispine file "
                f"id, so there is nothing to import it from — the clip is not "
                f"ingested and nothing is attached"
            )
            log.error(message)
            self.error = message
            return False

        running = list(in_flight_jobs or ())
        if running:
            # Not a failure, and not a second import either: the file IS
            # being imported, by a job this run did not start. Recording
            # it is what puts the job id back on the row the interrupted
            # run never wrote.
            log.info(
                f"Importing {self.item_id}: {main_file_id} is already being "
                f"imported by {', '.join(running)} from an earlier run — "
                f"recording that job instead of starting a second import"
            )
            self._record_job(running[0], job_helper)
            return True

        log.info(f"Importing {self.item_id}: Start importing single-component shape")

        res = ingest_helper.importFileToPlaceholder(
            self.item_id,
            file_id=main_file_id,
            ingestprofile_groups=user_groups,
            notification_id=None,
            noTranscode=no_transcode,
            ignore_sidecars=True,
        )

        job_id = job_id_from_response(res)
        if job_id:
            # Through `_record_job`, not the raw setter: the import has
            # already been SENT, so a `getJob` that answers None or raises
            # must cost a warning, never the id — a job-less row here is
            # a duplicate container import on the next run.
            self._record_job(job_id, job_helper)
            return True

        log.error(
            f"Importing {self.item_id}: single-component import response "
            f"carried no job id ({res!r}) — no import job was started"
        )
        return False

    def _count_media_components(
        self, main_file: Dict[str, Any], extra_files: List[Dict[str, Any]]
    ) -> Tuple[Optional[int], Optional[int]]:
        """The component budget the placeholder shape must declare.

        Every EXTRA file contributes one component of its own type. The
        MAIN file contributes whatever Vidispine's shape deduction
        extracts from it — a container component plus, NORMALLY, a video
        one.

        "Normally" is the whole point, and it is where this deliberately
        DEPARTS from Codemill's ``ItemHelper.importFileToPlaceholder``,
        which declares ``video=len(extraFileIds['video']) + 1`` flat. A
        source Vidispine cannot decode yields a ``binaryComponent``: it
        satisfies the container slot (proved by the 10 single-segment
        drop-frame clips, which promote normally) and fills NO video
        slot, so the extra slot is never filled and the shape stays a
        placeholder for ever — no ``original`` tag, no transcode, no
        error anywhere. Signature on all 31 stuck clips of the 2026
        STARLUX shoot: ``files == videoComponents + 1``,
        ``containerComponent`` absent, ``binaryComponent`` present.
        Codemill's own import path fails identically; do NOT "realign" it
        on their source without reintroducing this. (The anchor job of
        such a clip carries ``errorMessage = "Input stream index out of
        bounds"`` in its job data — read off VX-696024 on 2026-09-01, and
        the first explicit Vidispine error text we have for the
        non-deducible case. It is on the JOB, which is why no ingest ever
        surfaced it.)

        The other direction is just as wrong: declaring ``len(extras)``
        unconditionally makes a DEDUCIBLE anchor's own video component
        overflow the budget and Vidispine answers ``400 {"invalidInput":
        {"explanation": "No more components of that type is accepted",
        "value": "VIDEO_COMPONENT"}}`` (measured 2026-08-31). Hence a
        BRANCH, never a constant — and the PROVIDER owns the condition
        (``yields_video_component``), so this method knows nothing about
        codecs, timecodes or drop-frame flags.

        ``.get("type")``, not ``["type"]``: this method is also read by
        the diagnostic in ``_component_budget_report``, and a provider
        dict missing a key must not turn an error message into a
        ``KeyError``.

        Args:
            main_file: The anchor media file dictionary
            extra_files: The extra media file dictionaries

        Returns:
            Tuple of (audio_count, video_count), where counts are None if zero
        """
        importable = importable_extras(extra_files)
        audio_count = sum(file.get("type") == "audio" for file in importable)
        video_count = sum(file.get("type") == "video" for file in importable)

        # `importable_extras`, not the raw list: an extra the import loop
        # will not send must not be budgeted a slot nothing will fill —
        # which is defect A again, by another route.
        main_type = main_file.get("type") if isinstance(main_file, Mapping) else None
        if main_type == "audio":
            audio_count += 1
        elif main_type == "video" and yields_video_component(main_file):
            video_count += 1

        return (
            None if audio_count == 0 else audio_count,
            None if video_count == 0 else video_count,
        )

    def _placeholder_file_ids(self, item_helper: Any) -> Optional[Set[str]]:
        """The file ids currently attached to this item's ORIGINAL shape.

        ``None`` means "could not tell", which the wait treats exactly
        like a job it could not read: not landed.

        "No placeholder shape" is NOT by itself "could not tell". The
        shape query is a three-state FILTER (see ``ItemAPIEnhanced``:
        ``placeholder=true`` returns only placeholder shapes, the
        default returns only non-placeholder ones), so an item whose
        shape has been PROMOTED answers the first query with nothing —
        and its files are all there, which is the opposite of unknown.
        Telling the two apart costs a second query only in the case
        where the first came back empty, and reading a promoted shape's
        files as "unknown" would fail a clip whose import succeeded.
        """
        try:
            shapes = item_helper.getItemShapesFromNames(
                self.item_id, ["original"], placeholder=True
            )
            if not shapes:
                shapes = item_helper.getItemShapesFromNames(self.item_id, ["original"])
        except Exception as error:  # noqa: BLE001 - a read that fails is "unknown"
            log.warning(
                f"Importing {self.item_id}: cannot read the placeholder shape "
                f"({error}) — treating its components as not landed"
            )
            return None
        if not shapes:
            return None
        return {_file.getId() for _file in shapes[0].getAllFiles()}

    def _placeholder_still_open(self, item_helper: Any) -> Optional[bool]:
        """Whether this item's ORIGINAL shape is still a placeholder.

        ``True`` means a placeholder shape is there; ``False`` means the
        shape has been PROMOTED (no placeholder, a non-placeholder one
        in its place); ``None`` means "could not tell" — the query
        raised, or answered nothing on BOTH states of the three-state
        filter (see ``_placeholder_file_ids`` for the filter). The two
        queries are the same pair that method issues, for the same
        reason: "no placeholder shape" alone is not "promoted".
        """
        try:
            placeholders = item_helper.getItemShapesFromNames(
                self.item_id, ["original"], placeholder=True
            )
            if placeholders:
                return True
            promoted = item_helper.getItemShapesFromNames(self.item_id, ["original"])
        except Exception as error:  # noqa: BLE001 - a read that fails is "unknown"
            log.warning(
                f"Importing {self.item_id}: cannot read whether the placeholder "
                f"shape promoted ({error})"
            )
            return None
        return False if promoted else None

    def _in_flight_component_files(
        self, job_helper: Any, media_files: List[Dict[str, Any]]
    ) -> Dict[str, List[str]]:
        """This item's still-running placeholder imports, by media file id.

        The RESUME path's other half. A run that starts while the
        PREVIOUS run's component jobs are still in flight sees those
        files missing from the shape — the job has not attached them yet
        — and would import them a second time. Vidispine checks the
        budget at REQUEST time against the files already LANDED and
        never at landing (measured 2026-09-01 and 2026-09-02, with a
        control arm), so the duplicate is accepted and lands too: the
        shape ends with two components for one declared, SILENTLY. Only
        when the first has already landed does the second get the ``400 …
        _COMPONENT``; either way the resumable case turns into a failure
        the resume exists to avoid.

        Read off ``getAllJobsForItem``, which the plugin already uses
        (``Clip.jobs``), filtered to ``PLACEHOLDER_IMPORT``. Which
        component a job is importing is identified by its SOURCE PATH,
        not by its job data — see the comment above
        ``_normalised_media_path`` for the measurement that settles it.

        Three rules, in order, and the reason there are three is that the
        two sides name a file differently. ``getSourceFilePath()`` is an
        ABSOLUTE path under the storage root
        (``/mnt/PAD_Storage/AA - RUSHES TAPELESS/2026/…``) while a
        provider's ``path`` is what ``VSFile.getPath()`` gave it, which
        is storage-RELATIVE. So: exact match, then "the job's path ends
        with this file's path" (component-aligned, so ``…/a_002.R3D``
        never satisfies ``b_002.R3D``), then equal basenames. The last
        rule is loose on its own and safe here for two structural
        reasons: the jobs are already filtered to THIS item, and the
        candidates are already filtered to THIS clip's own media files —
        two of which never share a basename.

        FAIL-SAFE in every unknown: a listing that raises, a job that
        raises, a job with no readable source, a source matching nothing.
        None of them is skipped, so the caller falls back to today's
        behaviour (import it), whose worst case is the pre-existing 400 —
        loud, reported and recoverable — rather than a component that is
        never imported because an accessor was missing.

        Returns:
            ``{file_id: [job_id, ...]}`` for jobs still IN PROGRESS —
            every one of them, because a file with two running imports is
            the case that most needs waiting on.
        """
        candidates = [
            (
                _normalised_media_path(media_file.get("path")),
                media_file.get("file_id"),
            )
            for media_file in media_files or ()
            if isinstance(media_file, Mapping)
            and media_file.get("path")
            and media_file.get("file_id")
        ]
        if not candidates:
            return {}
        try:
            jobs = job_helper.getAllJobsForItem(
                self.item_id, job_type=PLACEHOLDER_IMPORT_JOB_TYPE
            )
        except (
            Exception
        ) as error:  # noqa: BLE001 - a listing that fails is "none known"
            log.warning(
                f"Importing {self.item_id}: cannot list the item's import jobs "
                f"({error}) — resuming on the attached files alone, so a "
                f"component whose job is still in flight may be re-imported "
                f"and land twice"
            )
            return {}
        in_flight: Dict[str, List[str]] = {}
        for job in jobs or ():
            try:
                # `_job_has_stopped` and NOT `inProgress()`: the latter
                # answers False for WAITING, so a component queued behind
                # a busy Vidispine looked finished and its file was
                # re-imported. An unknown answer is NOT counted as in
                # flight — the resume's fail-safe is to import, whose
                # worst case is the loud 400.
                if self._job_has_stopped(job) is not False:
                    continue
                job_id = job.getId()
                target = self._job_target_item(job)
                if target and target != self.item_id:
                    # `getAllJobsForItem` already filters, but a job that
                    # NAMES another item is not this item's by any
                    # reading, and skipping a component on its word would
                    # be the one unsafe direction here.
                    continue
                file_id = self._job_matches_a_media_file(job, candidates)
                if file_id is None:
                    continue
                # EVERY running job for the file, not the first. Duplicate
                # imports are the hazard this whole lookup exists for, so
                # a file with two jobs already running is exactly the case
                # that most needs both of them waited on — `setdefault`
                # dropped the second and the anchor could close the set
                # while it was still attaching.
                in_flight.setdefault(file_id, [])
                if job_id not in in_flight[file_id]:
                    in_flight[file_id].append(job_id)
            except (
                Exception
            ) as error:  # noqa: BLE001 - one unreadable job is not the listing
                log.warning(
                    f"Importing {self.item_id}: cannot read one of the item's "
                    f"import jobs ({error}) — it is not counted as in flight"
                )
        return in_flight

    @staticmethod
    def _job_status(job: Any) -> Optional[str]:
        """``getStatus()`` as an upper-case string, or ``None``."""
        accessor = getattr(job, "getStatus", None)
        if not callable(accessor):
            return None
        status = accessor()
        return str(status).strip().upper() if status else None

    @classmethod
    def _job_has_stopped(cls, job: Any) -> Optional[bool]:
        """Has this job reached a TERMINAL status?

        ``True`` terminal, ``False`` still coming, ``None`` "cannot tell"
        — which the two callers resolve in OPPOSITE directions, because
        the unsafe answer is not the same on both sides. The WAIT reads
        an unknown as still coming (closing the component set on an
        unknown outcome is what manufactures the unpromotable
        placeholder); the RESUME reads it as not in flight (refusing to
        import on an unknown would make one bad read permanently
        unresumable).

        `inProgress()` is only the FALLBACK, and never the rule: on this
        server it answers False for WAITING, which is an ordinary status
        on a busy Vidispine and precisely the state the wait exists for.
        """
        status = cls._job_status(job)
        if status in JOB_STATUSES_TERMINAL:
            return True
        if status in JOB_STATUSES_STILL_COMING:
            return False
        if status is not None:
            # A status neither set knows. Do not guess it into either
            # bucket — say so, and let each caller apply its own
            # fail-safe.
            return None
        accessor = getattr(job, "inProgress", None)
        if callable(accessor):
            return not accessor()
        return None

    @staticmethod
    def _job_target_item(job: Any) -> Optional[str]:
        """``getTargetItem()`` when the object has one, else ``None``."""
        accessor = getattr(job, "getTargetItem", None)
        if not callable(accessor):
            return None
        return accessor()

    @staticmethod
    def _job_matches_a_media_file(job: Any, candidates: List[Any]) -> Optional[str]:
        """The file id this job is importing, or ``None`` if it cannot say.

        ``candidates`` is ``[(normalised path, file_id), …]`` for THIS
        clip's media files only.
        """
        source = None
        accessor = getattr(job, "getSourceFilePath", None)
        if callable(accessor):
            source = _normalised_source_uri(accessor())
        filename = None
        accessor = getattr(job, "getFilename", None)
        if callable(accessor):
            filename = accessor() or None
        if source is None and filename is None:
            # Neither accessor exists or both are empty: this job says
            # nothing about which component it is importing.
            return None
        source_base = os.path.basename(source) if source else filename

        # The three rules are applied IN ORDER OVER ALL CANDIDATES, not
        # per candidate: evaluated inside one loop, a basename match on
        # an early candidate wins over an EXACT match on a later one,
        # which is the opposite of the documented precedence.
        usable = [(path, file_id) for path, file_id in candidates if path]
        if source is not None:
            for path, file_id in usable:
                if source == path:
                    return file_id
            for path, file_id in usable:
                # Component-ALIGNED suffix: the job's path is absolute
                # under the storage root, the provider's is relative to
                # it. `endswith(path)` alone would let `.../xa_002.R3D`
                # satisfy `a_002.R3D`.
                if source.endswith(os.sep + path):
                    return file_id
        if source_base:
            for path, file_id in usable:
                if source_base == os.path.basename(path):
                    return file_id
        return None

    def _component_job_running(
        self, job_id: str, job_helper: Any, unreadable: Set[str]
    ) -> bool:
        """Is this component's import job still running?

        "Running" means "has NOT reached a terminal status", read off
        ``getStatus()`` — never off ``inProgress()``, which on this
        server answers False for WAITING and would empty the pending set
        on the first pass for a component merely queued behind a busy
        Vidispine (`JOB_STATUSES_STILL_COMING`).

        A job that cannot be read counts as STILL RUNNING, and that is
        one rule with three arms now: ``getJob`` raising, ``getJob``
        answering ``None``, and a job whose status neither status set
        knows. "I could not tell" is not "it finished" — the bound stops
        the poll either way, and the alternative is closing the component
        set on a job whose outcome is unknown.

        ``unreadable`` dedupes the warning: a wedged Vidispine is polled
        many times and must not produce many identical lines.
        """
        try:
            job = job_helper.getJob(job_id)
        except Exception as error:  # noqa: BLE001 - any read failure is "unknown"
            if job_id not in unreadable:
                unreadable.add(job_id)
                log.warning(
                    f"Importing {self.item_id}: cannot read component job "
                    f"{job_id} ({error}) — treating it as still running"
                )
            return True
        if job is None:
            if job_id not in unreadable:
                unreadable.add(job_id)
                log.warning(
                    f"Importing {self.item_id}: component job {job_id} could not "
                    f"be found — treating it as still running"
                )
            return True
        stopped = self._job_has_stopped(job)
        if stopped is None:
            if job_id not in unreadable:
                unreadable.add(job_id)
                log.warning(
                    f"Importing {self.item_id}: component job {job_id} reports "
                    f"the unmodelled status {self._job_status(job)!r} — treating "
                    f"it as still running"
                )
            return True
        unreadable.discard(job_id)
        return not stopped

    def _wait_for_components_to_land(
        self,
        job_ids: List[str],
        expected_file_ids: Set[str],
        job_helper: Any,
        item_helper: Any,
        bound: float,
        bound_description: str = "",
    ) -> Optional[str]:
        """Block until every extra component has LANDED, or say why not.

        LANDED, not stopped. ``inProgress()`` is the vendor's own terminal
        test and it is right for "has this job stopped" and wrong for
        "did it work": it goes false for ``FAILED`` and ``ABORTED`` too,
        and treating those as success re-creates the incomplete component
        set this whole story removes. The honest check is the shape's own
        attached files, so both have to hold — every job stopped AND
        every file on the shape.

        ``JobHelper`` has no ``waitForJob`` (checked on the 6.2.1
        server), so this is a bounded poll. It dedupes the job ids, backs
        off, and logs only when the pending set CHANGES: a wedged
        Vidispine is precisely the case this runs for, and it must not be
        hammered or drown the report.

        Returns:
            ``None`` when everything landed, otherwise the operator-facing
            reason it did not.
        """
        deadline = time.monotonic() + bound
        pending = list(dict.fromkeys(job_ids))
        expected = set(expected_file_ids)
        interval = EXTRA_COMPONENT_POLL_SECONDS
        unreadable: Set[str] = set()
        confirmations = LANDING_CONFIRMATIONS
        reported = None

        attached = None
        missing = None
        last_pending_count = None

        while True:
            pending = [
                job_id
                for job_id in pending
                if self._component_job_running(job_id, job_helper, unreadable)
            ]
            # The shape is re-read only when the pending set SHRANK (or
            # emptied), never once per pass. Nothing but a job finishing
            # attaches a file, so a pass in which every job is still
            # running has nothing new to see — and the poll backs off
            # while the shape read did not, so a five-minute wait spent
            # one shape query every couple of seconds on an answer it
            # already had.
            if last_pending_count is None or len(pending) != last_pending_count:
                last_pending_count = len(pending)
                attached = self._placeholder_file_ids(item_helper)
                missing = sorted(expected - attached) if attached is not None else None

            if not pending and missing == []:
                return None

            if not pending:
                # Every job has reached a terminal status and the files
                # are not all there. One more look (a job can commit its
                # attachment between the two reads); after that it
                # stopped without landing.
                confirmations -= 1
                if confirmations > 0:
                    # Re-read next pass, whatever the pending count did.
                    last_pending_count = None
                if confirmations <= 0:
                    if missing is None:
                        # The shape could not be READ. Saying components
                        # "never attached although every job stopped"
                        # would name a cause this run never observed —
                        # and when `job_ids` was empty there were no jobs
                        # to stop in the first place.
                        return (
                            f"the placeholder shape could not be read, so "
                            f"whether this clip's {len(expected)} extra "
                            f"component(s) attached is unknown — the anchor is "
                            f"NOT imported, because closing a component set on "
                            f"an unknown state leaves a placeholder nothing can "
                            f"promote"
                        )
                    return (
                        f"{len(missing)} extra component(s) never attached their "
                        f"file"
                        + (
                            " although every import job has stopped"
                            if job_ids
                            else " and no import job was running for them"
                        )
                        + f" ({', '.join(missing)}) — a job that ends "
                        f"FAILED_TOTAL or ABORTED stops without attaching, and "
                        f"an anchor closing an incomplete component set leaves "
                        f"a placeholder nothing can promote"
                    )

            state = (tuple(pending), tuple(missing) if missing else ())
            if state != reported:
                reported = state
                log.info(
                    f"Importing {self.item_id}: waiting for {len(pending)} extra "
                    f"component job(s) to land before importing the anchor "
                    f"(jobs: {', '.join(pending) or 'none still running'}; "
                    f"files not yet attached: "
                    f"{', '.join(missing) if missing else 'unknown'})"
                )

            # Only a job that is STILL RUNNING can expire the bound. With
            # nothing pending the confirmation counter above already
            # guarantees termination, and letting the deadline pre-empt it
            # would report "0 jobs still running" for a job that failed.
            if pending and time.monotonic() >= deadline:
                return (
                    f"{len(pending)} extra component job(s) were still running "
                    f"after {bound:.0f}s{bound_description} "
                    f"({', '.join(pending) or 'none'}; files "
                    f"not yet attached: "
                    f"{', '.join(missing) if missing else 'unknown'}) — the "
                    f"anchor is NOT imported, because an anchor that closes an "
                    f"incomplete component set leaves a placeholder nothing can "
                    f"promote. The components already attached are kept, so the "
                    f"next run resumes this item rather than starting over"
                )

            if pending:
                # Clamped to what is LEFT of the bound: a 15 s ceiling on
                # a 30 s REST bound would otherwise overshoot the
                # deadline by most of a poll, and on that path the
                # overshoot is a request thread and its database
                # connection held past the budget the entry point set.
                # `_sleep`, not `time.sleep`: the seam tests patch is
                # this module's own name.
                _sleep(max(0.0, min(interval, deadline - time.monotonic())))
                interval = min(
                    interval * EXTRA_COMPONENT_POLL_BACKOFF,
                    EXTRA_COMPONENT_POLL_MAX_SECONDS,
                )
            else:
                # The CONFIRMATION floor, deliberately NOT clamped to the
                # remaining bound. Past the deadline the clamp above is
                # 0, so the confirmations would run back to back and
                # absorb none of the attachment race they exist for — the
                # one case they are here to get right. Bounded by
                # LANDING_CONFIRMATIONS x this.
                _sleep(LANDING_CONFIRMATION_SECONDS)

    def _import_multi_component(
        self,
        main_file: Dict[str, Any],
        extra_files: List[Dict[str, Any]],
        shape_id: str,
        user_groups: List[str],
        no_transcode: Optional[bool],
        user: Optional[User],
        item_helper: Any,
        job_helper: Any,
        attached_file_ids: FrozenSet[str] = frozenset(),
        component_wait_seconds: Optional[float] = None,
        component_wait_deadline: Optional[float] = None,
        in_flight_component_files: Optional[Dict[str, List[str]]] = None,
    ) -> bool:
        """Import a multi-component (with extra files) clip.

        Args:
            main_file: Main media file dictionary with 'file_id' and 'path'
            extra_files: List of extra media file dictionaries
            shape_id: Shape ID to import into
            user_groups: List of user groups for ingest profile
            no_transcode: Whether to skip transcoding
            user: User performing the operation
            item_helper: ItemHelperExtended instance
            job_helper: JobHelper instance
            attached_file_ids: Files a previous, unfinished import already
                attached to this placeholder — imported again they would
                duplicate a component and overflow the declared budget
            component_wait_seconds: How long to wait for the extra
                components to LAND before giving up on the anchor. The
                bound is per ENTRY POINT (``None`` = the scan's).
            component_wait_deadline: A ``time.monotonic()`` instant the
                per-clip bound is CLAMPED against — the whole-request
                budget ``views.py`` sets, so N clips of one request
                cannot cost N x the per-clip bound. ``None`` on the scan
                path, which holds nothing anyone is waiting on.
            in_flight_component_files: ``{file_id: [job_id, ...]}`` for this
                item's component imports still RUNNING from a previous
                run. Those files are not on the shape yet and must not be
                imported again — the duplicate would be accepted and land
                as a second component, silently (measured 2026-09-02) —
                but their jobs DO have to be waited for.

        Returns:
            True if import was successful
        """
        log.info(f"Importing {self.item_id}: Start importing multi-component shape")

        main_file_id = (
            main_file.get("file_id") if isinstance(main_file, Mapping) else None
        )
        if not main_file_id:
            # `getFileIdFromFullPath` answers None for a file Vidispine
            # does not know, and `red.getClipMainMediaFile` answers None
            # for a clip with no `file` — so this is reachable, and
            # sending `{"fileId": None}` would either 400 or, worse,
            # import something else. It is also what would make the
            # anchor's own file look FOREIGN to the resume, since
            # `_expected_file_ids` cannot name it.
            message = (
                f"Importing {self.item_id}: the anchor "
                f"{main_file.get('path') if isinstance(main_file, Mapping) else main_file!r} "
                f"has no Vidispine file id, so there is nothing to import it "
                f"from — the clip is not ingested and nothing is attached"
            )
            log.error(message)
            self.error = message
            return False
        nameless = extras_without_a_file_id(extra_files)
        if nameless:
            # A media-typed extra Vidispine has no file id for cannot be
            # imported, and until now it was simply filtered away: not
            # budgeted, not expected, not imported and not reported, so
            # the clip came back INGESTED with a span file missing from
            # the item. That is the same silence this whole story is
            # about, so it is a failure with a name.
            message = (
                f"Importing {self.item_id}: {len(nameless)} media file(s) of "
                f"this clip have no Vidispine file id "
                f"({', '.join(str(f.get('path')) for f in nameless)}) — they "
                f"cannot be imported, and ingesting the rest would leave the "
                f"item silently short of media. Nothing is imported; re-scan "
                f"once Vidispine knows the file(s)"
            )
            log.error(message)
            self.error = message
            return False

        # THE ANCHOR IS SUBJECT TO BOTH RESUME GUARDS TOO. It was not:
        # `attached_file_ids` and `in_flight` were consulted only inside
        # the extras loop and the anchor import was unconditional, so a
        # placeholder whose container import had landed (an interrupted
        # run whose anchor went first) was resumed and the anchor
        # imported a SECOND time — the duplicate component and the
        # measured 400 the resume exists to avoid.
        #
        # `_get_or_create_placeholder_shape` already refuses an attached
        # anchor a rung earlier, and this is the same verdict re-stated
        # where the import happens: the two are deliberately redundant,
        # because this method is also called directly and a future
        # refactor of the classifier must not silently re-open the hole.
        #
        # IT IS DECIDED FIRST, before the declaration and before a single
        # extra is imported. Stated after the extras loop — where it was
        # — the refusal was announced only once this run had already
        # re-declared the component count and sent every missing extra
        # import into a shape it was about to call a dead end: those
        # components LAND, and a landed component consumes a slot on a
        # placeholder nothing can ever promote. The verdict was right and
        # the harm was already done.
        #
        # `shape_id` is guarded in the message: on the direct-call path
        # the redundancy exists for, nothing guarantees a caller named
        # one. And the budget verdict is appended here too — it is
        # logged after `_count_media_components`, which this refusal now
        # precedes, so without it the one grep-able line naming the
        # declared set would be lost on exactly this exit.
        shape_description = f"shape {shape_id}" if shape_id else "its placeholder shape"
        if main_file_id and main_file_id in attached_file_ids:
            message = (
                f"Importing {self.item_id}: the anchor {main_file_id} is already "
                f"attached to {shape_description}, so its import job has already "
                f"evaluated this placeholder — importing it again would only "
                f"duplicate a component and be refused. Nothing re-evaluates a "
                f"placeholder, so this item cannot be promoted by a re-run"
                f"{self._component_budget_report(main_file, extra_files)}"
            )
            log.error(message)
            self.error = message
            return False

        # THE CLASSIFIER'S OTHER RUNG, re-stated for the same reason: a
        # shape holding a file that is not this clip's is not a resume of
        # this clip's import, and a direct caller handing such a shape in
        # must be refused BEFORE anything is imported into it — or this
        # clip's files are attached to another clip's item. The anchor's
        # own id is known here (checked above), so the expectation is
        # complete and a foreign file really is foreign.
        foreign = sorted(
            attached_file_ids - self._expected_file_ids(main_file, extra_files)
        )
        if foreign:
            message = (
                f"Importing {self.item_id}: {shape_description} holds "
                f"{len(foreign)} file(s) that are not this clip's "
                f"({', '.join(foreign)}), so this is not a resume of this "
                f"clip's import — nothing is imported into it"
                f"{self._component_budget_report(main_file, extra_files)}"
            )
            log.error(message)
            self.error = message
            return False

        # THE UN-EVIDENCED VERDICT IS REFUSED, NOT GUESSED (ruled
        # 2026-09-02, spec D6). A provider that has the question and no
        # answer — `red` on an unreadable `Abs TC` — declares `None`, and
        # a budget built on it is a guess in one of two directions:
        # over-declared, and the shape is a placeholder for ever with no
        # error anywhere (defect A); under-declared, and Vidispine
        # refuses the anchor with the 400. Neither is a declaration.
        # Before ANY import: the extras would otherwise land in a shape
        # whose budget nobody can vouch for. An ABSENT key is not this —
        # that is a provider that never had the question, and it keeps
        # the backward-compatible True (`yields_video_component`).
        #
        # Only when this run would DECLARE. A shape that already holds a
        # file keeps the declaration the run that started the import
        # made (below), so the verdict is not consulted on a resume — and
        # refusing there would strand an import whose budget was set by
        # a run that COULD read the metadata, on the strength of a
        # re-extraction that no longer can.
        if not attached_file_ids and main_file_verdict_is_unknown(main_file):
            main_file_path = main_file.get("path") or "(no path)"
            message = (
                f"Importing {self.item_id}: the provider could not tell whether "
                f"the anchor {main_file_path} contributes a video component of "
                f"its own (see the provider's warning for what it could not "
                f"read), so the component budget cannot be declared without "
                f"guessing. Over-declaring leaves the shape a placeholder for "
                f"ever and under-declaring is refused by Vidispine, so nothing "
                f"is imported. Fix the clip's metadata and re-run"
            )
            log.error(message)
            self.error = message
            return False

        query = {"fileId": main_file_id, "tag": "lowres"}
        if no_transcode:
            query["no-transcode"] = no_transcode

        in_flight = dict(in_flight_component_files or {})

        audio_count, video_count = self._count_media_components(main_file, extra_files)

        # The deduction verdict, on one grep-able line. Its ABSENCE is what
        # made the original diagnosis take three days: an over-declared
        # budget is invisible in every log the plugin wrote, because
        # nothing recorded what was declared or why.
        log.info(
            f"Importing {self.item_id}: component budget for anchor "
            f"{main_file.get('path')} — the provider says it "
            f"{'DOES' if yields_video_component(main_file) else 'does NOT'} "
            f"yield a video component of its own, so with {len(extra_files)} "
            f"extra file(s) the declaration is container=1, video={video_count}, "
            f"audio={audio_count}"
        )

        # Codemill declares the component count first, then imports every extra
        # component, and imports the main file LAST — the main import is what
        # closes the placeholder, so it must see a complete component set. Keep
        # that order. It differs on the count itself: Codemill only ever handles
        # spanned video (``video=len(extra) + 1``), whereas an extra here can be
        # a P2 audio track, so the count is taken per type.
        # DECLARE ON EVERY RUN THAT FINDS NO FILE ON THE SHAPE, and never
        # on a run that finds one. The rule is the measured one
        # (2026-09-01, with a control arm): a component slot is consumed
        # when a file LANDS, and re-declaring REPLACES the declaration
        # without giving a consumed slot back. So:
        #
        # * a file on the shape means a slot was consumed against the
        #   CURRENT declaration, and re-declaring a different budget
        #   under consumed slots is not measured — so we do not. The
        #   verdict really can move between runs (it is derived from
        #   `metadatas["timecode"]`, which a re-extraction rewrites, and
        #   an unreadable value is declared `None` — refused before any
        #   import on a first run, not consulted on a resume), and a
        #   budget overwritten under claimed slots is exactly the
        #   permanently unpromotable placeholder this story removes;
        # * a shape holding NO file has consumed nothing, so re-declaring
        #   is free — an in-flight job has claimed nothing yet, and there
        #   is no declaration any slot was consumed against. It is also
        #   REQUIRED: the previous run may have taken the SINGLE-component
        #   path (the provider saw no extras — an index/filesystem desync
        #   is a documented condition), which never declares at all, and
        #   left its container job in flight. Gating on that job as
        #   "prior work" skipped the declaration and imported the extras
        #   into a shape whose budget was never set — the unpromotable
        #   placeholder again, produced by the fix itself.
        #
        # Hence the ONLY evidence that a slot was consumed is a file
        # attached to the shape. The job listing is not consulted here.
        prior_run_declared = bool(attached_file_ids)
        if shape_id and prior_run_declared:
            log.info(
                f"Importing {self.item_id}: resuming into shape {shape_id}, "
                f"which already holds {len(attached_file_ids)} file(s) — a "
                f"landed file consumed a slot against the count declared by "
                f"the run that started this import, so that declaration is "
                f"kept rather than replaced (container=1, video={video_count}, "
                f"audio={audio_count} is what THIS run would have declared; "
                f"{len(in_flight)} file(s) still being imported by an earlier "
                f"run)"
            )
        elif shape_id:
            log.info(
                f"Importing {self.item_id}: Found shape {shape_id}, updating components count to {video_count} video components and {audio_count} audio component"
            )
            item_helper.itemapi.updatePlaceholderComponentCount(
                self.item_id,
                shape_id,
                container=1,
                video=video_count,
                audio=audio_count,
            )

        signals.vidispine_pre_ingest.send(
            sender=ItemHelper,
            instance=self.item_id,
            method="importFileToPlaceholder",
            query=query,
        )

        # Components carry no shape tag and no ingest profile, matching
        # Codemill's ``ItemHelper.importFileToPlaceholder``, which sends a bare
        # ``{'fileId': ...}`` for every extra component and puts the tag and the
        # ``jobmetadata`` groups on the main import alone. A tag here asks
        # Vidispine to derive a shape from one component in isolation — for a P2
        # clip that means transcoding a single audio track to a video preset.
        # ``ignore_sidecars`` has no Codemill counterpart: it postdates 2.2.0 and
        # is what the single-component path already passes.
        component_job_ids: List[str] = []
        component_failures: List[str] = []
        expected_file_ids: Set[str] = set()
        # COMPONENTS covered by a job — started by this run or found
        # running from an earlier one. Counted apart from the JOB ids,
        # because one job can cover several files and the anchor's job
        # can be in the list too: the operator-facing messages below
        # must not report a job count as a component count.
        components_with_a_job = 0
        already_attached = 0
        for extra_file in importable_extras(extra_files):
            expected_file_ids.add(extra_file["file_id"])
            if extra_file["file_id"] in attached_file_ids:
                # RESUME: a previous run already attached this component.
                # Importing it again would add a second component of the
                # same type and overflow the budget just declared.
                log.info(
                    f"Importing {self.item_id}: component "
                    f"{extra_file['file_id']}:{extra_file['path']} is already "
                    f"attached to {shape_description} — not importing it again"
                )
                already_attached += 1
                continue
            running_jobs = list(in_flight.get(extra_file["file_id"]) or ())
            if running_jobs:
                # RESUME, the other half: the previous run's import for
                # this component has not attached its file yet, but it is
                # still coming. Vidispine checks the budget at REQUEST
                # time only, against the files already LANDED (measured
                # 2026-09-02): a second import of the same file is
                # ACCEPTED while the first is in flight, and both land —
                # a duplicate component, silently; and it is the 400 if
                # the first has landed by then. Either way the resumable
                # case turns into a mess the resume exists to avoid. Wait
                # for the job that is already running instead of starting
                # a rival.
                log.info(
                    f"Importing {self.item_id}: component "
                    f"{extra_file['file_id']}:{extra_file['path']} is still "
                    f"being imported by {', '.join(running_jobs)} from an "
                    f"earlier run — waiting for that job instead of importing "
                    f"it again"
                )
                component_job_ids.extend(running_jobs)
                components_with_a_job += 1
                continue
            q = {"fileId": extra_file["file_id"]}
            log.info(
                f"Importing {self.item_id}: Import file {extra_file['file_id']}:{extra_file['path']} to component {extra_file['type']}..."
            )
            component_res = item_helper.itemapi.doImportToPlaceholder(
                item_id=self.item_id,
                query=q,
                component=extra_file["type"],
                runasuser=user,
                ignore_sidecars=True,
            )
            component_job_id = job_id_from_response(component_res)
            if component_job_id:
                log.info(f"... and got job {component_job_id}")
                component_job_ids.append(component_job_id)
                components_with_a_job += 1
            else:
                log.info("... but got no job in response")
                component_failures.append(
                    f"{extra_file['file_id']}:{extra_file['path']}"
                )

        anchor_jobs = list(in_flight.get(main_file_id) or ()) if main_file_id else []
        if anchor_jobs:
            # The anchor's own import is STILL RUNNING from an earlier
            # run. Starting a rival would duplicate the container
            # component; the honest move is to let that job finish and
            # see whether it promotes the shape.
            log.info(
                f"Importing {self.item_id}: the anchor {main_file_id} is still "
                f"being imported by {', '.join(anchor_jobs)} from an earlier "
                f"run — waiting for that job rather than starting a second "
                f"import"
            )
            if len(anchor_jobs) > 1:
                # Two running imports of the anchor is already the
                # duplicate this run refuses to add to. All are waited
                # for; the row can record only one, so say which.
                log.warning(
                    f"Importing {self.item_id}: {len(anchor_jobs)} import jobs "
                    f"are running for the anchor {main_file_id} "
                    f"({', '.join(anchor_jobs)}) — every one of them is waited "
                    f"for, and {anchor_jobs[0]} is the one recorded on the clip"
                )
            expected_file_ids.add(main_file_id)
            component_job_ids.extend(anchor_jobs)

        # ONE JOB ID, ONCE. `_in_flight_component_files` lists every
        # running import per FILE, so a single job reported for several
        # files lands here several times, and the anchor's own job can
        # already be in the list from the extras loop. The wait dedupes
        # internally, but the operator-facing message below COUNTS this
        # list — undeduped it over-states how many component jobs
        # actually started.
        component_job_ids = list(dict.fromkeys(component_job_ids))

        # A component whose import started no job will never attach its
        # file, so the set the anchor is about to close can never be
        # complete. Importing the anchor anyway is what manufactures the
        # unpromotable placeholder this method exists to stop producing.
        # The components that DID land stay attached, so the next run
        # resumes rather than starting over.
        if component_failures:
            message = (
                f"Importing {self.item_id}: {len(component_failures)} extra "
                f"component import(s) started no job "
                f"({', '.join(component_failures)}) — the anchor is NOT "
                f"imported, because an anchor closing an incomplete component "
                f"set leaves a placeholder nothing can ever promote. The next "
                f"run resumes this item"
            )
            log.error(message)
            self.error = message
            return False

        # THE WAIT (defect B). `doImportToPlaceholder` returns a job per
        # component and this loop used to log the id and drop it. It is
        # the ANCHOR's job that decides whether the placeholder is
        # complete, creates the shape and starts the transcode, so it must
        # not run before the last component has attached its file: on a
        # 29-clip batch (2026-08-31) the only 2 failures were the only 2
        # clips whose anchor job finished first. Codemill's code carries
        # the same race.
        #
        # An expired wait is a FAILURE, not a fallback: importing the
        # anchor anyway is precisely the race being removed.
        bound = (
            EXTRA_COMPONENT_WAIT_SECONDS
            if component_wait_seconds is None
            else component_wait_seconds
        )
        bound_description = ""
        if component_wait_deadline is not None:
            # The WHOLE-REQUEST budget. `views.py` ingests a folder, not
            # a clip, so the per-clip bound alone would let a 50-clip
            # folder hold a request thread and its database connection
            # for 50 x the bound. Each clip may spend the shorter of its
            # own bound and what is left of the request's.
            remaining = max(0.0, component_wait_deadline - time.monotonic())
            if remaining < bound:
                bound = remaining
                bound_description = (
                    ", which is all that was left of this request's "
                    "component-wait budget"
                )
        not_landed = self._wait_for_components_to_land(
            component_job_ids,
            expected_file_ids,
            job_helper,
            item_helper,
            bound,
            bound_description,
        )
        if not_landed:
            message = f"Importing {self.item_id}: {not_landed}"
            log.error(message)
            self.error = message
            return False

        if anchor_jobs:
            # Its own job did the import; there is nothing left for this
            # run to send. But LANDED is not PROMOTED: the wait answers
            # the first, and only the anchor's job answers the second —
            # and that job has already evaluated the placeholder, against
            # whatever had landed at that instant. Nothing re-evaluates a
            # placeholder (spec D7), so if the shape is still one now it
            # is a DEAD END, and counting the clip ingested would report
            # a success that will never come. Read the shape and say so,
            # LOUDLY — and do NOT record the job: `is_incomplete_import`
            # only revisits a clip with no job id, and the anchor-attached
            # rung above reports this item every run, as it should.
            anchor_job_list = ", ".join(anchor_jobs)
            still_open = self._placeholder_still_open(item_helper)
            if still_open is None:
                message = (
                    f"Importing {self.item_id}: the anchor was imported by "
                    f"{anchor_job_list}, which has now landed, but whether its "
                    f"job promoted the placeholder cannot be read (the shape "
                    f"query answered nothing on either state) — the next run "
                    f"re-examines this item"
                )
                log.error(message)
                self.error = message
                return False
            if still_open:
                message = (
                    f"Importing {self.item_id}: the anchor was imported by "
                    f"{anchor_job_list}, which has now landed, and the shape is "
                    f"still a placeholder — that job evaluated the placeholder "
                    f"before every component had landed, and nothing "
                    f"re-evaluates a placeholder, so this item cannot be "
                    f"promoted by a re-run. Its files are on the placeholder; "
                    f"the item must be deleted and the clip re-ingested"
                )
                log.error(message)
                self.error = message
                return False
            log.info(
                f"Importing {self.item_id}: the anchor was imported by "
                f"{anchor_job_list}, which has now landed and promoted the "
                f"shape — nothing more to send"
            )
            self._record_job(anchor_jobs[0], job_helper)
            return True

        log.info(
            f"Finally, import file {main_file_id} to item {self.item_id}...(user groups are {user_groups})"
        )
        res = item_helper.itemapi.doImportToPlaceholder(
            item_id=self.item_id,
            query=query,
            runasuser=user,
            ignore_sidecars=True,
            ingestprofile_groups=user_groups,
        )

        invalidate_item_cache(self.item_id)
        signals.vidispine_post_ingest.send(
            sender=ItemHelper,
            instance=self.item_id,
            method="importFileToPlaceholder",
        )

        job_id = job_id_from_response(res)
        if job_id:
            # Through `_record_job`: the anchor import has been SENT, so
            # an unfetchable job must cost a warning, never the id.
            self._record_job(job_id, job_helper)

        log.info(f"Retranscoding shape with item {self.item_id} and shape {shape_id}")

        # FR-36: no job id means Vidispine started no import job. Returning
        # True here — as this method unconditionally did — is how a clip
        # could be reported ingested with a NULL job_id and no import.
        if not job_id:
            message = (
                f"Importing {self.item_id}: the ANCHOR's import response carried "
                f"no job id ({res!r}), so nothing will evaluate the placeholder "
                f"— {components_with_a_job} extra component(s) are covered by "
                f"import job(s) {', '.join(component_job_ids) or 'none'}, "
                f"{already_attached} were already attached by an earlier run, "
                f"and their files stay on the placeholder, so the next run "
                f"resumes this item"
            )
            log.error(message)
            self.error = message
            return False
        return True

    def import_file(
        self,
        collection_id: Optional[str] = None,
        user: Optional[User] = None,
        replace: bool = False,
        legacy_storages: Optional[List[str]] = None,
        retry_incomplete: bool = False,
        component_wait_seconds: Optional[float] = None,
        component_wait_deadline: Optional[float] = None,
    ) -> Dict[str, bool]:
        """Import clip files into Vidispine, creating or updating an item.

        This method handles the complete import workflow including:
        - Creating or updating the Vidispine item
        - Handling file replacement for legacy storage migration
        - Importing single or multi-component media files
        - Managing placeholder shapes and transcoding

        Args:
            collection_id: Optional collection ID to add the item to
            user: User performing the import operation
            replace: Whether to replace existing original files
            legacy_storages: List of storage IDs considered as legacy for replacement
            retry_incomplete: Whether to look past "the item already
                exists" for a clip whose previous import created a
                placeholder and never started a job. Deliberately NOT
                ``replace``: it only lifts the blanket early return, so
                an item that really holds original files still comes back
                ``skipped`` instead of having its shape removed and
                re-imported.
            component_wait_seconds: How long a MULTI-component import may
                wait for its extra components to land before giving up on
                the anchor. ``None`` takes the scan's generous bound;
                ``views.py`` passes a short one, because a REST call holds
                a request thread and its database connection for the whole
                of the wait where the cron holds nothing anyone is
                waiting on.
            component_wait_deadline: The ``time.monotonic()`` instant the
                WHOLE request's component budget expires at, which every
                per-clip bound is clamped against. ``None`` on the scan
                path (see ``_import_multi_component``).

        Returns:
            Dictionary with status flags:
                - skipped: True if import was skipped
                - failed: True if import failed
                - replaced: True if original files were replaced
                - ingested: True if import succeeded
        """
        # A clip object can be imported twice in one process (the retry
        # rung, the tests' two-run scenarios), and `self.error` is what
        # `Folder.ingest` now reads to decide what reaches the operator's
        # report. Carrying the PREVIOUS attempt's reason into this one
        # would attribute an old failure to a new verdict.
        self.error = ""
        result = {
            "skipped": False,
            "failed": False,
            "replaced": False,
            "ingested": False,
        }

        _igh = TapelessIngestHelper(runas=user)
        _ijh = JobHelper(runas=user)
        _ith = ItemHelperExtended(runas=user)
        _gh = GroupHelper(runas=user)
        _ch = CollectionHelper(runas=user)
        _sh = StorageHelper(runas=user)
        # Per-call, like every helper above (story 3.1). This call site
        # passes all four explicitly and always did — `_ith` in
        # particular is an `ItemHelperExtended`, which no default could
        # supply. What changed is on the other side: `create_item`'s
        # `gh`/`uh`/`ith`/`ch` now DEFAULT to None and are built per call
        # as the run's user (retro-3 F4), where they used to be four
        # shared instances built at class-definition time with
        # runas=None. Passing them here is no longer what saves a future
        # caller from that — but it is still what puts the subclass and
        # this run's user in.
        _uh = UserHelper(runas=user)

        # Create item if it doesn't exist
        _, created = self.create_item(
            user=user,
            collection_id=collection_id,
            replace=replace,
            ith=_ith,
            gh=_gh,
            uh=_uh,
            ch=_ch,
        )
        if not replace and not created and not retry_incomplete:
            log.info("Item already exists, skipping it")
            result["skipped"] = True
            return result

        # Handle existing item replacement
        if self.item is not None:
            if not self.file:
                log.info(
                    f"Importing {self.item_id}: No file to replace with, skipping it"
                )
                result["skipped"] = True
                return result

            original_shapes = _ith.getItemShapesFromNames(self.item_id, ["original"])
            if original_shapes:
                for original_shape in original_shapes:
                    original_files = original_shape.getAllFiles()

                    if not self._should_replace_original_files(
                        original_files, replace, legacy_storages
                    ):
                        result["skipped"] = True
                        return result

                    self._remove_original_shapes(user, _ith, _sh)
                    result["replaced"] = True

        self.user = user
        self.status = self.STATUS_PLACHOLDER_CREATED

        # Get media files and import options
        extra_files = self.provider.getClipAdditionalMediaFiles(self)
        main_file = self.provider.getClipMainMediaFile(self)
        options = self.provider.getImportOptions()
        main_file_id = main_file["file_id"]

        no_transcode = options.get("no-transcode", None)
        if result["replaced"]:
            no_transcode = True

        _, default_ingest_group = _gh.getUserIngestGroups()
        user_groups = [urllib.parse.quote(str(default_ingest_group))]

        placeholder = self._get_or_create_placeholder_shape(
            user, _ith, main_file=main_file, extra_files=extra_files
        )
        shape_id = placeholder.shape_id
        if shape_id is None:
            result["failed"] = True
            return result

        # A shape this call just MINTED cannot have an earlier run's
        # import jobs pointing at it, so the job listing is bought only
        # for an item that could actually be mid-resume — which is what
        # keeps the happy path at exactly the Vidispine calls it made
        # before this story.
        #
        # It is computed for BOTH branches. Computed only in the
        # multi-component one, it left the single-component path outside
        # the resume guards entirely: an interrupted import leaves a
        # placeholder that is still EMPTY while its job runs, the empty
        # placeholder is reused silently, and the `retry_incomplete` rung
        # brings the next scan straight back here to fire a second
        # container import. The ANCHOR is in the list either way — it is
        # the one import that cannot be duplicated safely.
        in_flight = (
            {}
            if placeholder.created
            # `importable_extras`, the SAME filter the import loop and
            # `_expected_file_ids` use: a non-media extra (`xdcam`'s
            # `metadatas` dict) is never imported, so a running job
            # matching its basename must not be read as one of this
            # clip's component imports.
            else self._in_flight_component_files(
                _ijh, list(importable_extras(extra_files)) + [main_file]
            )
        )

        # Import based on component count
        if len(extra_files) == 0:
            imported = self._import_single_component(
                main_file_id,
                user_groups,
                no_transcode,
                _igh,
                _ijh,
                in_flight_jobs=in_flight.get(main_file_id) or (),
            )
        else:
            imported = self._import_multi_component(
                main_file,
                extra_files,
                shape_id,
                user_groups,
                no_transcode,
                user,
                _ith,
                _ijh,
                attached_file_ids=placeholder.attached_file_ids,
                component_wait_seconds=component_wait_seconds,
                component_wait_deadline=component_wait_deadline,
                in_flight_component_files=in_flight,
            )

        # FR-36: an import with no job id is a FAILURE, never an ingest.
        # The unconditional `result["ingested"] = True` that used to close
        # this method reported success for exactly the responses the two
        # helpers had just rejected.
        if imported:
            result["ingested"] = True
        else:
            # Truthful: the multi-component path can now fail with several
            # component jobs STARTED, so the old flat "no import job was
            # started" would have been a lie about half the failures. The
            # helper's own reason wins whenever it recorded one.
            log.error(
                f"Importing {self.item_id}: counting this clip failed — "
                f"{self.error or 'no import job was started'}"
            )
            result["failed"] = True
        return result

    # The columns an ingest owns, and the only ones it writes back. Every
    # column `import_file` mutates is here: item_id (create_item's
    # placeholder), job_id (the import job), status, user — plus file_id,
    # which the scan attached and the deleted full save() also persisted.
    #
    # `user_id`, not `user`: reading `self.user` on a row whose FK is not
    # already loaded issues a SELECT to fetch the User object, once per
    # clip, inside the submission loop the whole AD-6 effort exists to
    # keep query-free — and the UPDATE only ever needed the id. The two
    # write the same column.
    INGEST_STATE_FIELDS = ("item_id", "job_id", "status", "file_id", "user_id")

    def ingest(
        self,
        collection_id: Optional[str] = None,
        user: Optional[User] = None,
        folder: Optional[Any] = None,
        replace: bool = False,
        legacy_storages: Optional[List[str]] = None,
        expect_persisted: bool = True,
        retry_incomplete: bool = False,
        component_wait_seconds: Optional[float] = None,
        component_wait_deadline: Optional[float] = None,
    ) -> Dict[str, bool]:
        """Convenience method that wraps import_file for ingest operations.

        Args:
            collection_id: Optional collection ID to add the item to
            user: User performing the ingest operation
            folder: Folder object containing the clip
            replace: Whether to replace existing original files
            legacy_storages: List of storage IDs considered as legacy for replacement
            expect_persisted: Whether the caller guarantees the clip's row
                already exists (the scan path does; the REST endpoint,
                which ingests a clip built from a request body, does not)
            retry_incomplete: Whether this clip's last import left a
                placeholder and no job (see ``import_file``)
            component_wait_seconds: The entry point's wait bound for a
                multi-component import (see ``import_file``)
            component_wait_deadline: The whole-request budget every
                per-clip bound is clamped against (see ``import_file``)

        Returns:
            Dictionary with status flags from import_file operation
        """
        result = self.import_file(
            user=user,
            collection_id=collection_id,
            replace=replace,
            legacy_storages=legacy_storages,
            retry_incomplete=retry_incomplete,
            component_wait_seconds=component_wait_seconds,
            component_wait_deadline=component_wait_deadline,
        )
        self.persist_ingest_state(expect_persisted=expect_persisted)
        if folder:
            # M2M row insert: a declared AD-6 deviation, dry-run gated by
            # the caller, tracked for 2.7 / the coordinator.
            self.folders.add(folder)
        return result

    def persist_ingest_state(self, expect_persisted: bool = True) -> None:
        """Write back what the import decided, and nothing else (AD-6).

        ONE targeted UPDATE of ``INGEST_STATE_FIELDS``. Second writer of
        ``item_id``/``status``: ``create_item``'s combined write already
        made the placeholder durable mid-import (story 3.0); this one
        re-writes both alongside the job id. A full ``save()``
        would rewrite every column of a row the scan just wrote,
        including the location columns the scan deliberately leaves alone
        (``scan.persistence.CLIP_UPDATE_FIELDS``).

        Two ways the row may not be there: an unsaved clip (the REST
        endpoint builds one from the request body — legitimate, hence
        ``expect_persisted``), or a row that vanished between the scan's
        write unit and now. Both fall back to ``save()``: an ingest whose
        state is not persisted is an ingest that runs again next scan.
        """
        if not self._state.adding:
            updated = (
                type(self)
                .objects.filter(pk=self.pk)
                .update(
                    **{
                        field: getattr(self, field)
                        for field in self.INGEST_STATE_FIELDS
                    }
                )
            )
            if updated:
                return
            log.error(
                f"clip {self.umid}: ingest-state UPDATE matched no row — the "
                f"row was deleted concurrently; re-inserting it so the ingest "
                f"is not silently lost"
            )
        elif expect_persisted:
            log.error(
                f"clip {self.umid} reached ingest unsaved — the scan's write "
                f"unit should have written it"
            )
        self.save()

    def persist_metadatas(self) -> None:
        """Write this clip's memoized metadatas, batched.

        The SAME code path the scan write unit uses — a bounded number of
        statements whatever the key count, instead of one upsert per
        metadata key plus a delete. A clip that never had metadatas
        assigned writes nothing at all; an EMPTY metadatas mapping still
        clears the clip's rows.
        """
        if not hasattr(self, "_metadatas"):
            return
        writes = (MetadataWrite(umid=self.pk, metadatas=self._metadatas),)
        # Atomic like its scan-path twin (models/folder.persist_scan_results):
        # the upsert and the stale-key DELETE are one edit of this clip's
        # key-set, and half of it is worse than none. Nested inside a
        # caller's transaction this is a savepoint, so a failure here
        # cannot poison an outer request transaction.
        with transaction.atomic():
            type(self).persist_metadatas_bulk(writes, group_stale_deletes(writes))

    @classmethod
    def persist_metadatas_bulk(
        cls,
        writes: Any,
        stale_deletes: Any,
    ) -> None:
        """Upsert every clip's metadata rows, then drop the stale ones.

        ``writes`` must hold at most one entry per umid — the plan layer
        (``scan.persistence.dedupe_candidates``) guarantees it, and a
        repeated conflict target inside one ``ON CONFLICT`` statement is
        rejected by both sqlite and PostgreSQL. ``stale_deletes`` comes
        from ``group_stale_deletes`` over the SAME writes, so the two can
        never disagree about which keys survive.
        """
        rows = [
            ClipMetadata(clip_id=write.umid, name=name, value=value)
            for write in writes
            for name, value in write.metadatas.items()
        ]
        if rows:
            # batch_size, not "one statement whatever the size": a folder
            # accumulates candidates across every page, and one card tree
            # can carry more placeholders than PostgreSQL's 65535
            # bind-parameter ceiling — which fails the whole folder's
            # write, not just the overflowing rows.
            ClipMetadata.objects.bulk_create(
                rows,
                update_conflicts=True,
                unique_fields=["clip", "name"],
                update_fields=["value"],
                batch_size=BULK_BATCH_SIZE,
            )
        for stale in stale_deletes:
            for umids in chunked(stale.umids, BULK_BATCH_SIZE):
                ClipMetadata.objects.filter(clip_id__in=umids).exclude(
                    name__in=stale.keep_names
                ).delete()


class ClipMetadata(models.Model):
    clip = models.ForeignKey(Clip, max_length=100, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    value = models.CharField(max_length=200, blank=True, null=True, default="")

    class Meta:
        unique_together = (("clip", "name"),)

    def __unicode__(self):
        return "%s" % self.value


class ClipFile(models.Model):
    clip = models.ForeignKey(Clip, max_length=100, on_delete=models.CASCADE)
    path = models.TextField()
    filetype = models.CharField(max_length=100)
    order = models.IntegerField(blank=True, default=0)

    class Meta:
        unique_together = (("clip", "path"),)


class SpannedClips(models.Model):
    master_clip = models.ForeignKey(
        Clip,
        related_name="spanned_clips",
        max_length=100,
        on_delete=models.CASCADE,
    )
    clip = models.ForeignKey(Clip, max_length=100, on_delete=models.CASCADE)
    order = models.IntegerField(blank=True, default=0)

    class Meta:
        unique_together = (("master_clip", "order"),)
        ordering = ["order"]
