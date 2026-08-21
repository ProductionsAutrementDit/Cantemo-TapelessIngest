"""Pure ingest-decision ladder: which clips may cost a Vidispine call.

Stdlib-only by contract (AD-1), like ``scan.context``, ``scan.extraction``,
``scan.verification`` and ``scan.persistence``: no Django, no ORM, no
provider and no helper imports. Clips travel through it duck-typed — only
``umid``, ``provider_name`` and ``item_id`` are ever read.

This is the DECISION half of the ingest phase, and only the decision half:
executing an ingest lives in ``models/clip.py`` / ``models/folder.py``.

* **the recovery gate** (``needs_hash_recovery``) — a legacy-storage hash
  lookup is worth an HTTP call only for a clip with no ``item_id``, a
  hashed file and configured legacy storages (FR-8).
* **the will-ingest selection** (``will_ingest`` /
  ``select_clips_to_ingest``) — an already-ingested clip is discovered
  BEFORE ``create_item``'s item fetch, placeholder and group calls, and
  before the folder resolves its collection (FR-23, AD-15).
* **umid dedup** (``dedupe_clips_by_umid``) — the umid IS the clip primary
  key, so two files resolving to one umid are one clip and must produce
  ONE ingest. Same rule as the write side's ``dedupe_candidates``: first
  appearance's slot, last occurrence's object.

NFR-1, the hash rule
--------------------
The file hash is the dedup key: it is what proves a file is not already in
Vidispine under another storage. A file Cantemo has not hashed yet is
NEVER matched against legacy storages and NEVER ingested in this run —
ingesting it blind risks a duplicate item, while skipping it costs one
cron cycle. It is bucketed ``skipped`` with a logged retry-next-run
reason, not an error and not an exception.

The incomplete-import rung
--------------------------
``item_id`` presence is what FR-23 calls already-ingested, but an import
that answered without a job id leaves a row that LOOKS ingested and is
not: ``create_item`` assigned the placeholder's id before the import was
attempted, so the row carries an ``item_id``, placeholder status and a
NULL ``job_id``. Skipping on ``item_id`` alone strands those clips
forever. ``is_incomplete_import`` names exactly that state, and the
caller re-ingests it with ``replace=True`` — the one path that can import
into an existing placeholder. It is deliberately narrower than "no job
id": a clip whose ``item_id`` was RECOVERED from a legacy storage also
has no job, and re-examining those would buy back the per-clip HTTP cost
FR-8 exists to remove.

Caller contract for ``has_hash``
--------------------------------
``has_hash`` is computed by the CALLER, from the scan-cached file only
(``Clip._file``) — never from the ``Clip.file`` property, which issues a
``getFileById`` HTTP call when the memo is cold. A ladder that reached
for the property would spend one call per clip to decide it must not
spend any.
"""

__all__ = [
    "SKIP_ALREADY_INGESTED",
    "SKIP_NO_HASH",
    "dedupe_clips_by_umid",
    "is_incomplete_import",
    "needs_hash_recovery",
    "select_clips_to_ingest",
    "will_ingest",
]

# Why a clip the ladder rejected was rejected. Stable tokens, not prose:
# the caller owns the wording of the log line and the counter it feeds.
SKIP_ALREADY_INGESTED = "already_ingested"
SKIP_NO_HASH = "no_hash"


def needs_hash_recovery(item_id, file_hash, legacy_storages) -> bool:
    """Is a legacy-storage hash lookup worth any HTTP call for this file?

    False — i.e. zero calls — in all three settled cases: ``item_id``
    truthy (nothing left to recover, which is what makes a re-scan free),
    ``file_hash`` falsy (no dedup key, so no lookup may be attempted at
    all — NFR-1), or no ``legacy_storages`` (nowhere to look).
    """
    if item_id:
        return False
    if not file_hash:
        return False
    if not legacy_storages:
        return False
    return True


def is_incomplete_import(item_id, job_id, at_placeholder_status) -> bool:
    """The exact state a job-id-less import leaves behind.

    An ``item_id`` (the placeholder ``create_item`` made), no ``job_id``
    (no import job was ever started) and the status that only
    ``import_file`` writes. All three together: a clip that would
    otherwise be skipped as already-ingested for the rest of its life.

    ``at_placeholder_status`` is passed as a bool by the caller — the
    status constant belongs to the model, not to this module.
    """
    return bool(item_id) and not job_id and bool(at_placeholder_status)


def will_ingest(*, item_id, replace, has_hash, import_incomplete=False) -> bool:
    """Would ingesting this clip do any work in Vidispine?

    False when the clip already carries an ``item_id`` and this is not a
    ``replace`` run — FR-23 defines already-ingested as item_id presence,
    and ``import_file`` would have discovered exactly that, several HTTP
    calls later. False as well for a hash-less clip, whatever its
    ``item_id`` state (NFR-1). True again when the id belongs to an
    incomplete import (see the module docstring).

    A stale ``item_id`` (item deleted in Vidispine) is still skipped by
    this rung — ruled correct for now, detection is deferred work.
    """
    if not has_hash:
        return False
    if item_id and not replace and not import_incomplete:
        return False
    return True


def dedupe_clips_by_umid(clips_with_state):
    """One entry per umid: first appearance's slot, LAST occurrence's clip.

    The umid is the clip's primary key, so two scanned files carrying the
    same umid are one clip by definition and must produce one ingest —
    whether or not the double call would have been caught Vidispine-side.
    Last-wins is not arbitrary: it is the rule the write side already
    applies (``scan.persistence.dedupe_candidates``), so the object that
    gets ingested is the one whose state the row reflects.

    The scan RESPONSE keeps every clip it collected; dedup applies to
    what is acted on.
    """
    deduped = {}
    for clip, has_hash, import_incomplete in clips_with_state:
        deduped[clip.umid] = (clip, has_hash, import_incomplete)
    return list(deduped.values())


def select_clips_to_ingest(clips_with_state, *, providers, replace):
    """Split scanned clips into "ingest these" and "skipped, because".

    ``clips_with_state`` is an iterable of
    ``(clip, has_hash, import_incomplete)`` triples — see the module
    docstring for where ``has_hash`` must come from. It is deduped by
    umid here, so a caller cannot forget to.

    ``providers``, when not None, is the run's provider-name filter. A
    clip filtered out by it is not "skipped": it was never this run's
    business, and today's ingest loop does not count it either. It
    appears in neither returned sequence.

    Returns:
        ``(to_ingest, skipped_reasons)`` — the clips to ingest in input
        order, and ``(clip, reason)`` pairs for the ones the ladder
        rejected, also in input order.
    """
    to_ingest = []
    skipped_reasons = []
    for clip, has_hash, import_incomplete in dedupe_clips_by_umid(clips_with_state):
        if providers is not None and clip.provider_name not in providers:
            continue
        if will_ingest(
            item_id=clip.item_id,
            replace=replace,
            has_hash=has_hash,
            import_incomplete=import_incomplete,
        ):
            to_ingest.append(clip)
            continue
        skipped_reasons.append(
            (clip, SKIP_NO_HASH if not has_hash else SKIP_ALREADY_INGESTED)
        )
    return to_ingest, skipped_reasons
