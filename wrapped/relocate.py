"""Align an applied row on a shoot folder renamed after P5 archived it.

P5 knows the renamed folder; the row's VX-41 entities were registered
under the old one. ``relocate_file`` does not rename in place (measured
on VX-10456): Vidispine replaces the entity with a NEW one, state OPEN,
already swapped into the shape's components. So each original is
relocated, looked up again by its new path, checked to be the entity
the item's original shape now names, and set back to ARCHIVED.

Before any write, a read-only pre-pass looks at EVERY original still
under the old folder (planned entity at the old path, no entity already
at the new one) and verifies the row as it stands: a row that already
fails ``verify_item`` is not this command's to touch.

Resumable file by file: the plan is saved after EACH file and remembers
where the file came from (``relocated_from``). An original whose old
path is gone while its new path exists (a run killed between the two
writes) only gets the ARCHIVED check; so does every original this
command relocated before, on each later run, until the row verifies.
"""

import copy
from typing import List, Mapping

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.executor import StepError
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item

ARCHIVED = "ARCHIVED"
OPEN = "OPEN"
RELOCATED_FROM = "relocated_from"


class PreexistingVerifyFailure(StepError):
    """The row fails ``verify_item`` and this command never wrote to it:
    no original carries ``relocated_from`` and no killed run left an
    entity at a new path."""


def under(plan: Mapping, prefix: str) -> bool:
    return any(o["relative"].startswith(prefix) for o in plan.get("originals", []))


def relocated(plan: Mapping, old: str, new: str) -> List[Mapping]:
    """The originals this command already moved from ``old`` to ``new``."""
    return [
        o
        for o in plan.get("originals", [])
        if o["relative"].startswith(new) and o.get(RELOCATED_FROM, "").startswith(old)
    ]


class Relocator:
    def __init__(self, gateway, old: str, new: str, persist: bool = True):
        self.gateway = gateway
        self.old = old
        self.new = new
        self.persist = persist
        # Only filled by a dry run: the Clip updates it did not make.
        self.planned_clip_updates: List[dict] = []
        # What the operator should look at, printed by the command.
        self.notes: List[str] = []
        self.rechecked = 0

    def _new_relative(self, old_rel: str) -> str:
        return self.new + old_rel[len(self.old) :]

    def run(self, row) -> int:
        """Relocate the row's originals under ``old``; the count moved."""
        if not self.persist:
            row = copy.copy(row)
            row.plan = copy.deepcopy(row.plan)
        pending = [
            o for o in row.plan["originals"] if o["relative"].startswith(self.old)
        ]
        done = relocated(row.plan, self.old, self.new)
        self.rechecked = len(done)
        if pending:
            self._preflight(row, pending)
        for original in pending:
            self._relocate(row, original)
        for original in done:
            self._ensure_archived(original["file_id"])
        if self.persist:
            # A dry run cannot verify DRYRUN ids Vidispine never saw.
            problems = verify_item(row, self.gateway)
            if problems:
                raise StepError("; ".join(problems))
        return len(pending)

    def _preflight(self, row, pending) -> None:
        """Every read that can refuse the row, before its first write."""
        projected = copy.copy(row)
        projected.plan = copy.deepcopy(row.plan)
        by_relative = {o["relative"]: o for o in projected.plan["originals"]}
        resumed: List[str] = []
        for original in pending:
            old_rel = original["relative"]
            new_rel = self._new_relative(old_rel)
            at_old = self.gateway.find_file(fields.RUSHES_STORAGE, old_rel)
            at_new = self.gateway.find_file(fields.RUSHES_STORAGE, new_rel)
            if at_old is not None:
                if at_old.file_id != original["file_id"]:
                    raise StepError(
                        f"VX-41 entity at {old_rel} is {at_old.file_id}, not the "
                        f"planned {original['file_id']}"
                    )
                if at_new is not None:
                    raise StepError(
                        f"conflict: entity already at new path {new_rel} "
                        f"({at_new.file_id}) while {old_rel} is still "
                        f"{at_old.file_id}"
                    )
            elif at_new is None:
                raise StepError(f"no VX-41 entity at {old_rel} nor at {new_rel}")
            else:
                # A run killed after relocate_file: judge the row as it is.
                by_relative[old_rel]["file_id"] = at_new.file_id
                resumed.append(at_new.file_id)
        problems = verify_item(projected, self.gateway)
        if not problems:
            return
        touched = any(o.get(RELOCATED_FROM) for o in row.plan["originals"])
        if not touched and not resumed:
            raise PreexistingVerifyFailure("; ".join(problems))
        # This command already wrote to the row: its failure, not a
        # pre-existing one. Never leave a killed run's entity OPEN.
        if self.persist:
            for file_id in resumed:
                state = self.gateway.file_state(fields.RUSHES_STORAGE, file_id)
                if state == OPEN:
                    self.gateway.set_file_state(
                        fields.RUSHES_STORAGE, file_id, ARCHIVED
                    )
        raise StepError(
            "verify fails on a row this command already moved: " + "; ".join(problems)
        )

    def _relocate(self, row, original) -> None:
        old_rel = original["relative"]
        new_rel = self._new_relative(old_rel)
        found = self.gateway.find_file(fields.RUSHES_STORAGE, old_rel)
        if found is not None:
            self.gateway.relocate_file(fields.RUSHES_STORAGE, found.file_id, new_rel)
        moved = self.gateway.find_file(fields.RUSHES_STORAGE, new_rel)
        if moved is None:
            raise StepError(f"no VX-41 entity at {new_rel} after relocating {old_rel}")
        referenced = set()
        for shape in self.gateway.original_shapes(row.item_id):
            referenced |= shape.file_ids()
        if moved.file_id not in referenced:
            raise StepError(
                f"VX-41 entity {moved.file_id} at {new_rel} is not in an "
                f"original shape of {row.item_id}"
            )
        self._ensure_archived(moved.file_id)
        # The clip and originalFilename follow the FIRST original, as apply's
        # _update_clip: a P2 take's video, or a ``file`` item's one original,
        # which may be an audio WAV.
        if original is row.plan["originals"][0]:
            self._update_video(row, original, new_rel, moved.file_id)
        original["file_id"] = moved.file_id
        original["relative"] = new_rel
        original[RELOCATED_FROM] = old_rel
        if self.persist:
            row.save(update_fields=["plan", "updated_on"])

    def _ensure_archived(self, file_id: str) -> None:
        state = self.gateway.file_state(fields.RUSHES_STORAGE, file_id)
        if state is None:
            raise StepError(f"VX-41 entity {file_id} is gone")
        if state != ARCHIVED:
            self.gateway.set_file_state(fields.RUSHES_STORAGE, file_id, ARCHIVED)

    def _update_video(self, row, original, new_rel: str, file_id: str) -> None:
        old_rel = original["relative"]
        name = fields.ITEM_ORIGINAL_FILENAME_FIELD
        current = self.gateway.item_fields(row.item_id, [name]).get(name)
        if current == [old_rel]:
            self.gateway.set_item_metadata(row.item_id, {name: new_rel})
        elif current != [new_rel]:
            self.notes.append(f"{name} left as {current}")
        if not self.persist:
            self.planned_clip_updates.append(
                {"umid": row.clip_umid, "file_id": file_id}
            )
            return
        # Only a Clip still on the old video (or already on the new one, on
        # a resumed run): one fixed by hand since is a failure, not a target.
        updated = Clip.objects.filter(
            umid=row.clip_umid, file_id__in=(original["file_id"], file_id)
        ).update(file_id=file_id)
        if updated != 1:
            raise StepError(
                f"clip update matched {updated} row(s) for umid "
                f"{row.clip_umid!r} on file {original['file_id']}, expected 1"
            )
