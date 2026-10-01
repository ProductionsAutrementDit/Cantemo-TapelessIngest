"""Apply one row's plan, one idempotent phase at a time.

``row.phase`` is the last phase that FINISHED and is saved after each
one, so a killed run resumes at the next phase. Every phase re-checks
Vidispine before writing: ``shape/create`` ADDS shapes (measured,
VX-216897), so the shape phase first looks for an original shape that
already names these files.

Manual undo (no code performs this): ``PUT
/API/item/{item_id}/shape/{wrapped_shape_id}/tag/original`` restores the
``original`` tag this executor removed from the wrapped shape; remove the
``original`` tag from the newly posted shape (``row.plan["new_shape_id"]``);
then restore the Clip row from ``rollback["clip"]`` (``output_file``,
``file_id``, ``status``, ``job_id``) and restore EVERY value in
``rollback["item_fields"]`` onto the item. This now includes the technical
fields ``shape/create?updateItemMetadata=true`` also rewrites (measured on
prod, M10, 2026-09-29): originalFilename, originalFormat,
originalVideoCodec, originalAudioCodec, originalWidth, originalHeight,
mimeType, mediaType, durationTimeCode, startTimeCode and startSeconds,
alongside durationSeconds. For a spanned take (a plan with ``manifest``)
that also clears ``portal_pad_assembly``, which was empty before.
"""

import copy
from typing import Callable, Dict, List, Optional, Tuple

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.metadata import (
    component_fields,
    item_fields,
)
from portal.plugins.TapelessIngest.wrapped.shape import (
    build_copy_document,
    build_document,
    build_document_from_template,
    build_span_document,
)
from portal.plugins.TapelessIngest.wrapped.templates import Timing
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item

WRAP_PHASES = (
    "files_registered",
    "shape_posted",
    "metadata_written",
    "old_shape_removed",
    "clip_updated",
    "verified",
    "done",
)
COMPLETE_PHASES = ("metadata_written", "verified", "done")


class StepError(Exception):
    """A phase found Vidispine in a state its plan did not expect."""


class Executor:
    def __init__(
        self,
        gateway,
        disk,
        persist: bool = True,
        delete_online_wrapped: bool = False,
    ):
        self.gateway = gateway
        self.disk = disk
        self.persist = persist
        # Opt-in: by default an online wrapped file is kept and flagged.
        self.delete_online_wrapped = delete_online_wrapped
        # Only ever filled by a dry run (persist=False): what _update_clip
        # would have written, since it cannot touch the Clip table itself.
        self.planned_clip_updates: List[dict] = []

    def _steps(self, row) -> List[Tuple[str, Callable]]:
        steps = {
            "files_registered": self._register_files,
            "shape_posted": self._post_shape,
            "metadata_written": self._write_metadata,
            "old_shape_removed": self._detach_wrapped,
            "clip_updated": self._update_clip,
            "verified": self._verify,
            "done": self._finish,
        }
        names = WRAP_PHASES if row.plan["kind"] == "wrap" else COMPLETE_PHASES
        return [(name, steps[name]) for name in names]

    def run(self, row, stop_before: Optional[str] = None) -> None:
        steps = self._steps(row)
        names = [name for name, _ in steps]
        if stop_before is not None and stop_before not in names:
            raise ValueError(
                f"stop_before={stop_before!r} does not name a phase of this "
                f"row's plan ({', '.join(names)})"
            )
        if not self.persist:
            # A dry run must never mutate the caller's row: a later save,
            # or a real run on the same object, would otherwise carry the
            # DRYRUN file/shape ids this run invents.
            row = copy.copy(row)
            row.plan = copy.deepcopy(row.plan)
        start = names.index(row.phase) + 1 if row.phase else 0
        for name, step in steps[start:]:
            if name == stop_before:
                return
            if name == "verified" and not self.persist:
                # A dry run cannot verify DRYRUN ids the real gateway never
                # wrote; skip the check but still fall through to "done" so
                # a would-be delete is recorded.
                pass
            else:
                step(row)
            row.phase = name
            if self.persist:
                row.save(update_fields=["phase", "plan", "updated_on"])

    # phases
    def _register_files(self, row) -> None:
        # Plan-time on_disk can be stale. Re-probed before ANY write: an
        # archived original gone since plan is registered ARCHIVED (no
        # sha1); an unarchived one stops the row untouched. A plan-time
        # file_id usually names an ONLINE VX-41 entity found by path while
        # the file was still on disk: if the file is gone now, reusing that
        # binding is exactly what the planner's F1 gate would have refused
        # had it known — so the row must stop, not carry it to "done".
        for original in row.plan["originals"]:
            if original["on_disk"] and not self.disk.exists(original["relative"]):
                if not original["entry"]:
                    raise StepError(
                        f"on-disk original {original['relative']} disappeared "
                        f"since plan and is not in P5"
                    )
                if original["file_id"]:
                    raise StepError(
                        f"on-disk original {original['relative']} disappeared "
                        f"since plan and is bound to VX-41 entity "
                        f"{original['file_id']}; re-plan"
                    )
                original["on_disk"] = False
        # Read-only pre-pass: every original's VX-41 entity is looked up
        # and its state checked BEFORE any register_file call, so a stale
        # entity on the LAST original still stops the row with zero
        # writes rather than surfacing only after the earlier originals
        # were already registered.
        found_ids: Dict[int, str] = {}
        for index, original in enumerate(row.plan["originals"]):
            if original["file_id"]:
                continue
            found = self.gateway.find_file(fields.RUSHES_STORAGE, original["relative"])
            if found is None:
                continue
            if not original["on_disk"] and found.state != "ARCHIVED":
                raise StepError(
                    f"VX-41 entity {found.file_id} ({found.state}) for "
                    f"tape-only original {original['relative']}; re-plan"
                )
            found_ids[index] = found.file_id
        for index, original in enumerate(row.plan["originals"]):
            if original["file_id"]:
                continue
            if index in found_ids:
                original["file_id"] = found_ids[index]
            else:
                original["file_id"] = self.gateway.register_file(
                    fields.RUSHES_STORAGE,
                    original["relative"],
                    archived=not original["on_disk"],
                )

    def _file_ids(self, row, kind: str) -> List[str]:
        return [o["file_id"] for o in row.plan["originals"] if o["kind"] == kind]

    def _post_shape(self, row) -> None:
        wanted = frozenset(o["file_id"] for o in row.plan["originals"])
        live = self.gateway.original_shapes(row.item_id)
        for shape in live:
            if shape.file_ids() == wanted:
                row.plan["new_shape_id"] = shape.shape_id
                return
        # shape/create ADDS a shape: never POST onto an item whose originals
        # are no longer exactly the planned wrapped shape.
        live_ids = [s.shape_id for s in live]
        if live_ids != [row.plan["wrapped_shape_id"]]:
            raise StepError(
                f"original shapes are {live_ids}, expected only the wrapped "
                f"{row.plan['wrapped_shape_id']}"
            )
        if "segments" in row.plan:
            document = self._span_document(row.plan)
            row.plan["new_shape_id"] = self.gateway.post_shape(row.item_id, document)
            return
        if row.plan.get("technical_source") == "copy":
            # A wrapped ``file`` item: its one original holds every stream,
            # which may be audio only (a WAV), so no video id is assumed.
            document = build_copy_document(
                parse_shape(row.plan["wrapped_shape"]),
                row.plan["originals"][0]["file_id"],
            )
            row.plan["new_shape_id"] = self.gateway.post_shape(row.item_id, document)
            return
        video_id = self._file_ids(row, "video")[0]
        audio_ids = self._file_ids(row, "audio")
        # The planner stored the template and timing: apply never reads
        # p2_templates.json. A row planned before technical_source existed
        # was always restated from its wrapped shape.
        if row.plan.get("technical_source", "wrapped").startswith("template:"):
            document = build_document_from_template(
                row.plan["template"],
                video_id,
                audio_ids,
                Timing(**row.plan["timing"]),
            )
        else:
            document = build_document(
                parse_shape(row.plan["wrapped_shape"]), video_id, audio_ids
            )
        row.plan["new_shape_id"] = self.gateway.post_shape(row.item_id, document)

    @staticmethod
    def _span_document(plan) -> dict:
        """A spanned take: one shape naming every segment's files, grouped
        by the ``segment`` index the planner put on each original."""
        count = len(plan["segments"])
        videos: List[str] = [""] * count
        audios: List[List[str]] = [[] for _ in range(count)]
        for original in plan["originals"]:
            if original["kind"] == "video":
                videos[original["segment"]] = original["file_id"]
            else:
                audios[original["segment"]].append(original["file_id"])
        if plan["technical_source"].startswith("template:"):
            return build_span_document(
                plan["segments"],
                videos,
                audios,
                template=plan["template"],
                timing=Timing(**plan["timing"]),
            )
        return build_span_document(
            plan["segments"],
            videos,
            audios,
            wrapped=parse_shape(plan["wrapped_shape"]),
        )

    def _write_metadata(self, row) -> None:
        shape_id = row.plan["new_shape_id"]
        shapes = [
            s
            for s in self.gateway.original_shapes(row.item_id)
            if s.shape_id == shape_id
        ]
        if not shapes:
            raise StepError(f"original shape {shape_id} is not on {row.item_id}")
        by_file = {o["file_id"]: o for o in row.plan["originals"]}
        for original in row.plan["originals"]:
            if original["on_disk"] and "sha1" not in original:
                original["sha1"] = self.disk.sha1(original["relative"])
        for component in shapes[0].components:
            file_id = component.files[0].file_id if component.files else None
            original = by_file.get(file_id)
            if original is None:
                raise StepError(
                    f"component {component.component_id} names {file_id}, "
                    f"which is not one of the planned originals"
                )
            self.gateway.set_component_metadata(
                row.item_id,
                shape_id,
                component.component_id,
                component_fields(original, original.get("sha1")),
            )
        summary = item_fields(row.plan["originals"])
        if "manifest" in row.plan:
            summary[fields.PAD_ASSEMBLY_FIELD] = row.plan["manifest"]
        if summary:
            self.gateway.set_item_metadata(row.item_id, summary)

    def _detach_wrapped(self, row) -> None:
        wrapped_id = row.plan["wrapped_shape_id"]
        if any(
            s.shape_id == wrapped_id for s in self.gateway.original_shapes(row.item_id)
        ):
            self.gateway.untag_shape(row.item_id, wrapped_id, fields.ORIGINAL_TAG)

    def _update_clip(self, row) -> None:
        # The first original: a P2 take's video (always listed first), or a
        # wrapped ``file`` item's one original, which may be an audio WAV.
        file_id = row.plan["originals"][0]["file_id"]
        if not self.persist:
            self.planned_clip_updates.append(
                {
                    "umid": row.clip_umid,
                    "file_id": file_id,
                    "output_file": None,
                    "status": Clip.STATUS_SHAPE_POSTED,
                    "job_id": "",
                }
            )
            return
        updated = Clip.objects.filter(umid=row.clip_umid).update(
            file_id=file_id,
            output_file=None,
            status=Clip.STATUS_SHAPE_POSTED,
            job_id="",
        )
        if updated != 1:
            raise StepError(
                f"clip update matched {updated} row(s) for umid "
                f"{row.clip_umid!r}, expected 1"
            )

    def _verify(self, row) -> None:
        problems = verify_item(row, self.gateway)
        if problems:
            raise StepError("; ".join(problems))

    def _finish(self, row) -> None:
        # One wrapped file (``wrapped_file``, every row planned before
        # copies existed), one per online legacy storage (``wrapped_files``),
        # or none at all (a fileless original shape: nothing to do).
        if "wrapped_files" in row.plan:
            wrapped_files = row.plan["wrapped_files"]
        else:
            single = row.plan.get("wrapped_file")
            wrapped_files = [single] if single else []
        for wrapped in wrapped_files:
            self._finish_file(row, wrapped)

    def _finish_file(self, row, wrapped: Dict[str, str]) -> None:
        if wrapped["storage_id"] not in fields.ONLINE_LEGACY_STORAGES:
            return
        if not self.delete_online_wrapped:
            row.plan["wrapped_kept"] = True
            return
        # Re-checked against Vidispine, never decided from the state the
        # plan captured: a resumed run must not delete an already-deleted
        # file (or one Vidispine otherwise moved offline since planning).
        # Only an allowlisted online state is deleted; None (gone) or any
        # other state keeps the file.
        state = self.gateway.file_state(wrapped["storage_id"], wrapped["file_id"])
        if state not in fields.ONLINE_STATES:
            return
        self.gateway.delete_file(wrapped["storage_id"], wrapped["file_id"])
