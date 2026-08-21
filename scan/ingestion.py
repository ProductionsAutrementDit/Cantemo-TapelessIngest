"""Pure ingest-decision ladder: which clips may cost a Vidispine call.

Stdlib-only by contract (AD-1), like ``scan.context``, ``scan.extraction``,
``scan.verification`` and ``scan.persistence``: this module must import
with no Portal stub installed, so it holds no Django, no ORM, no provider
and no helper imports. Clips travel through it duck-typed — only
``provider_name`` and ``item_id`` are ever read.

This is the DECISION half of the ingest phase, and only the decision half:
executing an ingest still lives in ``models/clip.py`` /
``models/folder.py`` (story 2.8 owns the phase composition). What moves
here is the ladder those two used to spell out inline, where every rung
was paid for in HTTP calls:

* **the recovery gate** (``needs_hash_recovery``) — the legacy-storage
  hash lookup used to run for EVERY scanned file, before the clip was
  even looked up in the DB, so a clip that has been ingested for years
  still paid one ``getFilesInStorage`` per legacy storage on every scan
  (FR-8). Recovery answers exactly one question — "does this file already
  have an item somewhere?" — which is settled the moment the clip has an
  ``item_id``.
* **the will-ingest selection** (``will_ingest`` /
  ``select_clips_to_ingest``) — an already-ingested clip used to be
  discovered as such only INSIDE ``import_file``, after
  ``create_item``'s item fetch, placeholder and group calls had already
  gone out, and after the folder had resolved its collection (FR-23,
  AD-15).

NFR-1, the hash rule
--------------------
The file hash is the dedup key: it is what proves a file is not already
in Vidispine under another storage. A file Cantemo has not hashed yet is
therefore NEVER matched against legacy storages and NEVER ingested in
this run — ingesting it blind risks a duplicate item, while skipping it
costs one cron cycle. It is bucketed ``skipped`` with a logged
retry-next-run reason, not an error and not an exception (the pre-2.5
code raised ``TapelessIngestException("No hash found in file ...")``,
which cost the file its whole scan record).

Caller contract for ``has_hash``
--------------------------------
``has_hash`` is computed by the CALLER, from the scan-cached file only
(``Clip._file``, seeded by ``attach_file_metadatas``) — never from the
``Clip.file`` property, which issues a ``getFileById`` HTTP call when the
memo is cold. A ladder that reached for the property would spend one call
per clip to decide it must not spend any.
"""

__all__ = [
    "SKIP_ALREADY_INGESTED",
    "SKIP_NO_HASH",
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

    False — i.e. zero calls — in all three settled cases:

    * ``item_id`` truthy: the clip already has its item, there is nothing
      to recover (this is the one that makes a re-scan free);
    * ``file_hash`` falsy: no dedup key, so no lookup may be attempted at
      all (NFR-1);
    * no ``legacy_storages`` configured: there is nowhere to look.
    """
    if item_id:
        return False
    if not file_hash:
        return False
    if not legacy_storages:
        return False
    return True


def will_ingest(item_id, replace, has_hash: bool) -> bool:
    """Would ingesting this clip do any work in Vidispine?

    False when the clip already carries an ``item_id`` and this is not a
    ``replace`` run — FR-23 defines already-ingested as item_id presence,
    and ``import_file`` would have discovered exactly that, several HTTP
    calls later. False as well for a hash-less clip, whatever its
    ``item_id`` state (NFR-1): without the dedup key an ingest could
    duplicate an item that is already there.

    A stale ``item_id`` (item deleted in Vidispine) is skipped by this
    rung too — ruled correct for now, detection is deferred work.
    """
    if not has_hash:
        return False
    if item_id and not replace:
        return False
    return True


def select_clips_to_ingest(clips_with_hash, providers, replace):
    """Split scanned clips into "ingest these" and "skipped, because".

    ``clips_with_hash`` is an iterable of ``(clip, has_hash)`` pairs — see
    the caller contract in the module docstring for where ``has_hash``
    must come from.

    ``providers``, when not None, is the run's provider-name filter. A
    clip filtered out by it is not "skipped": it was never this run's
    business, and today's ingest loop does not count it either. It appears
    in neither returned sequence.

    Returns:
        ``(to_ingest, skipped_reasons)`` — the clips to ingest in input
        order, and ``(clip, reason)`` pairs for the ones the ladder
        rejected, also in input order.
    """
    to_ingest = []
    skipped_reasons = []
    for clip, has_hash in clips_with_hash:
        if providers is not None and clip.provider_name not in providers:
            continue
        if will_ingest(clip.item_id, replace, has_hash):
            to_ingest.append(clip)
            continue
        skipped_reasons.append(
            (clip, SKIP_NO_HASH if not has_hash else SKIP_ALREADY_INGESTED)
        )
    return to_ingest, skipped_reasons
