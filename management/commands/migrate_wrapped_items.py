"""Re-point legacy wrapped P2 items to their original files.

Spec: _bmad-output/implementation-artifacts/spec-wrapped-items-migration-p2.md

  plan    read-only; one WrappedMigration row per item, with its verdict
  apply   ready / already-migrated rows, one resumable phase at a time
  verify  re-checks every finished row
  report  counts per verdict/phase, and the errors

Never interactive. A row whose phase is non-empty is frozen against plan.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped import verdicts
from portal.plugins.TapelessIngest.wrapped.archive import (
    CachedArchive,
    load_archive_lookup,
)
from portal.plugins.TapelessIngest.wrapped.disk import Disk
from portal.plugins.TapelessIngest.wrapped.dryrun import RecordingGateway
from portal.plugins.TapelessIngest.wrapped.executor import Executor
from portal.plugins.TapelessIngest.wrapped.planner import PlanResult, plan_item
from portal.plugins.TapelessIngest.wrapped.resolver import (
    ResolveError,
    resolve_p2,
    wrapped_p2_clips,
)
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item


def _vidispine_gateway():
    # Imported lazily: it is the only module that needs a live Portal.
    from portal.plugins.TapelessIngest.wrapped.vidispine import VidispineGateway

    return VidispineGateway()


class Command(BaseCommand):
    help = "Migrate legacy wrapped P2 items to their original files."

    gateway_factory = staticmethod(_vidispine_gateway)
    archive_factory = staticmethod(load_archive_lookup)
    disk_factory = Disk

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("plan", "apply", "verify", "report"))
        parser.add_argument("--item", dest="item_id")
        parser.add_argument("--collection", dest="collection_id")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--dryrun", action="store_true")

    def handle(self, *args, **options):
        if options["limit"] is not None and options["limit"] < 1:
            raise CommandError("--limit must be a positive integer")
        if options["dryrun"] and options["action"] != "apply":
            raise CommandError("--dryrun only applies to 'apply'")
        getattr(self, f"_{options['action']}")(options)

    def _rows(self, options):
        rows = WrappedMigration.objects.all()
        if options["item_id"]:
            rows = rows.filter(item_id=options["item_id"])
        if options["collection_id"]:
            umids = Clip.objects.filter(collection_id=options["collection_id"]).values(
                "umid"
            )
            rows = rows.filter(clip_umid__in=umids)
        return rows.order_by("item_id")

    def _plan(self, options):
        archive = CachedArchive(self.archive_factory())
        gateway, disk = self.gateway_factory(), self.disk_factory()
        clips = wrapped_p2_clips(options["item_id"], options["collection_id"])
        if options["limit"]:
            clips = clips[: options["limit"]]
        counts = Counter()
        for clip in clips:
            existing = WrappedMigration.objects.filter(item_id=clip.item_id).first()
            if existing and existing.phase:
                counts["frozen"] += 1
                continue
            try:
                result = plan_item(
                    item_id=clip.item_id,
                    originals=resolve_p2(clip),
                    spanned=clip.spanned,
                    output_file=clip.output_file,
                    gateway=gateway,
                    archive=archive,
                    disk=disk,
                )
            except ResolveError as error:
                result = PlanResult(verdicts.UNEXPECTED, str(error))
            except Exception as error:  # noqa: BLE001 - reclassified next plan
                result = PlanResult(verdicts.ERROR, f"{type(error).__name__}: {error}")
            if result.rollback:
                result.rollback["clip"] = {
                    "output_file": clip.output_file,
                    "file_id": clip.file_id,
                    "status": clip.status,
                    "job_id": clip.job_id,
                }
            WrappedMigration.objects.update_or_create(
                item_id=clip.item_id,
                defaults={
                    "clip_umid": clip.umid,
                    "verdict": result.verdict,
                    "reason": result.reason,
                    "plan": result.plan,
                    "rollback": result.rollback,
                    "error": "",
                },
            )
            counts[result.verdict] += 1
        for name, count in sorted(counts.items()):
            self.stdout.write(f"{name}: {count}")

    def _apply(self, options):
        gateway, disk = self.gateway_factory(), self.disk_factory()
        dry = options["dryrun"]
        rows = (
            self._rows(options)
            .filter(verdict__in=verdicts.WRITABLE)
            .exclude(phase="done")
        )
        if options["limit"]:
            rows = rows[: options["limit"]]
        done = failed = 0
        for row in rows:
            used = RecordingGateway(gateway) if dry else gateway
            executor = Executor(used, disk, persist=not dry)
            try:
                executor.run(row)
            except Exception as error:  # noqa: BLE001 - isolate the item
                failed += 1
                row.error = f"{row.phase or 'start'}: {type(error).__name__}: {error}"
                if not dry:
                    row.save(update_fields=["error", "updated_on"])
                self.stdout.write(f"{row.item_id}: FAILED {row.error}")
                continue
            done += 1
            if dry:
                for write in used.writes:
                    self.stdout.write(f"{row.item_id}: {write[0]} {write[1:]}")
                for update in executor.planned_clip_updates:
                    self.stdout.write(f"{row.item_id}: clip_update {update}")
            elif row.error:
                row.error = ""
                row.save(update_fields=["error", "updated_on"])
        self.stdout.write(f"applied: {done}, failed: {failed}")

    def _verify(self, options):
        gateway = self.gateway_factory()
        for row in self._rows(options).filter(phase="done"):
            problems = verify_item(row, gateway)
            self.stdout.write(
                f"{row.item_id}: " + ("ok" if not problems else "; ".join(problems))
            )

    def _report(self, options):
        rows = self._rows(options)
        counts = Counter((r.verdict, r.phase or "-") for r in rows)
        for (verdict, phase), count in sorted(counts.items()):
            self.stdout.write(f"{verdict}/{phase}: {count}")
        for row in rows.exclude(error="")[:50]:
            self.stdout.write(f"ERROR {row.item_id}: {row.error}")
