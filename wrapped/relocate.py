"""Align an applied row on a shoot folder renamed after P5 archived it.

P5 knows the renamed folder; the row's VX-41 entities were registered
under the old one. ``relocate_file`` does not rename in place (measured
on VX-10456): Vidispine replaces the entity with a NEW one, state OPEN,
already swapped into the shape's components. So each original is
relocated, looked up again by its new path, and set back to ARCHIVED.

Resumable file by file: the plan is saved after EACH file, and an
original whose old path is gone while its new path exists (a run killed
between the two writes, or a file moved by hand) only gets the ARCHIVED
check.
"""

import copy
from typing import List, Mapping

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.executor import StepError
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item

ARCHIVED = "ARCHIVED"


def under(plan: Mapping, prefix: str) -> bool:
    return any(o["relative"].startswith(prefix) for o in plan.get("originals", []))


class Relocator:
    def __init__(self, gateway, old: str, new: str, persist: bool = True):
        self.gateway = gateway
        self.old = old
        self.new = new
        self.persist = persist
        # Only filled by a dry run: the Clip updates it did not make.
        self.planned_clip_updates: List[dict] = []

    def run(self, row) -> int:
        """Relocate the row's originals under ``old``; the count moved."""
        if not self.persist:
            row = copy.copy(row)
            row.plan = copy.deepcopy(row.plan)
        moved = 0
        for original in row.plan["originals"]:
            if not original["relative"].startswith(self.old):
                continue
            self._relocate(row, original)
            moved += 1
        if self.persist:
            # A dry run cannot verify DRYRUN ids Vidispine never saw.
            problems = verify_item(row, self.gateway)
            if problems:
                raise StepError("; ".join(problems))
        return moved

    def _relocate(self, row, original) -> None:
        old_rel = original["relative"]
        new_rel = self.new + old_rel[len(self.old) :]
        found = self.gateway.find_file(fields.RUSHES_STORAGE, old_rel)
        if found is not None:
            if found.file_id != original["file_id"]:
                raise StepError(
                    f"VX-41 entity at {old_rel} is {found.file_id}, not the "
                    f"planned {original['file_id']}"
                )
            self.gateway.relocate_file(fields.RUSHES_STORAGE, found.file_id, new_rel)
        moved = self.gateway.find_file(fields.RUSHES_STORAGE, new_rel)
        if moved is None:
            raise StepError(f"no VX-41 entity at {new_rel} after relocating {old_rel}")
        if self.gateway.file_state(fields.RUSHES_STORAGE, moved.file_id) != ARCHIVED:
            self.gateway.set_file_state(fields.RUSHES_STORAGE, moved.file_id, ARCHIVED)
        if original["kind"] == "video":
            self._update_video(row, old_rel, new_rel, moved.file_id)
        original["file_id"] = moved.file_id
        original["relative"] = new_rel
        if self.persist:
            row.save(update_fields=["plan", "updated_on"])

    def _update_video(self, row, old_rel: str, new_rel: str, file_id: str) -> None:
        name = fields.ITEM_ORIGINAL_FILENAME_FIELD
        current = self.gateway.item_fields(row.item_id, [name]).get(name)
        if current == [old_rel]:
            self.gateway.set_item_metadata(row.item_id, {name: new_rel})
        if not self.persist:
            self.planned_clip_updates.append(
                {"umid": row.clip_umid, "file_id": file_id}
            )
            return
        updated = Clip.objects.filter(umid=row.clip_umid).update(file_id=file_id)
        if updated != 1:
            raise StepError(
                f"clip update matched {updated} row(s) for umid "
                f"{row.clip_umid!r}, expected 1"
            )
