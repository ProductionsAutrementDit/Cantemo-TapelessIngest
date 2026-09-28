"""Apply one row's plan, one idempotent phase at a time.

``row.phase`` is the last phase that FINISHED and is saved after each
one, so a killed run resumes at the next phase. Every phase re-checks
Vidispine before writing: ``shape/create`` ADDS shapes (measured,
VX-216897), so the shape phase first looks for an original shape that
already names these files.
"""

import copy
from typing import Callable, List, Optional, Tuple

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.metadata import (
    component_fields,
    item_fields,
)
from portal.plugins.TapelessIngest.wrapped.shape import build_document
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
        # sha1); an unarchived one stops the row untouched.
        for original in row.plan["originals"]:
            if original["on_disk"] and not self.disk.exists(original["relative"]):
                if not original["entry"]:
                    raise StepError(
                        f"on-disk original {original['relative']} disappeared "
                        f"since plan and is not in P5"
                    )
                original["on_disk"] = False
        for original in row.plan["originals"]:
            if original["file_id"]:
                continue
            found = self.gateway.find_file(fields.RUSHES_STORAGE, original["relative"])
            original["file_id"] = (
                found.file_id
                if found
                else self.gateway.register_file(
                    fields.RUSHES_STORAGE,
                    original["relative"],
                    archived=not original["on_disk"],
                )
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
        document = build_document(
            parse_shape(row.plan["wrapped_shape"]),
            self._file_ids(row, "video")[0],
            self._file_ids(row, "audio"),
        )
        row.plan["new_shape_id"] = self.gateway.post_shape(row.item_id, document)

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
        if summary:
            self.gateway.set_item_metadata(row.item_id, summary)

    def _detach_wrapped(self, row) -> None:
        wrapped_id = row.plan["wrapped_shape_id"]
        if any(
            s.shape_id == wrapped_id for s in self.gateway.original_shapes(row.item_id)
        ):
            self.gateway.retag_shape(
                row.item_id,
                wrapped_id,
                add=fields.LEGACY_WRAPPED_TAG,
                remove=fields.ORIGINAL_TAG,
            )

    def _update_clip(self, row) -> None:
        video_file_id = self._file_ids(row, "video")[0]
        if not self.persist:
            self.planned_clip_updates.append(
                {
                    "umid": row.clip_umid,
                    "file_id": video_file_id,
                    "output_file": None,
                    "status": Clip.STATUS_SHAPE_POSTED,
                    "job_id": "",
                }
            )
            return
        updated = Clip.objects.filter(umid=row.clip_umid).update(
            file_id=video_file_id,
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
        wrapped = row.plan.get("wrapped_file")
        if not wrapped or wrapped["storage_id"] not in fields.ONLINE_LEGACY_STORAGES:
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
