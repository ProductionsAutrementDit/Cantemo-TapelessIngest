"""Re-point legacy wrapped P2 items to their original files.

Spec: _bmad-output/implementation-artifacts/spec-wrapped-items-migration-p2.md

  plan    read-only; one WrappedMigration row per item, with its verdict
  apply   ready / already-migrated rows, one resumable phase at a time;
          needs --item, --collection, --limit or an explicit --all, and
          stops after --max-failures (default 20) failures in a row
  verify  re-checks every finished row
  report  counts per verdict/phase, why items are not acted on, the ready
          rows per technical source, and the errors; ``--verdict V`` lists
          each item of that verdict instead
  templates
          read-only (no Vidispine, no P5, no DB write): learns one P2
          technical template per format from the genuine ready rows and
          writes it as JSON to --out (default: stdout, the per-format
          summary then going to stderr)

Never interactive. A row whose phase is non-empty is frozen against plan.
"""

import json
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata

# Registers `Folder`, which `Clip.folders` references lazily. Portal's plugin
# loading does not import it (plugin.py never reaches views), so without this
# import Django's system checks fail with fields.E307 before `handle` runs —
# measured on prod 2026-09-28. The scan commands import it the same way.
from portal.plugins.TapelessIngest.models.folder import Folder  # noqa: F401
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped import verdicts
from portal.plugins.TapelessIngest.wrapped.archive import (
    CachedArchive,
    load_archive_lookup,
)
from portal.plugins.TapelessIngest.wrapped.disk import Disk
from portal.plugins.TapelessIngest.wrapped.dryrun import RecordingGateway
from portal.plugins.TapelessIngest.wrapped.executor import Executor
from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.planner import PlanResult, plan_item
from portal.plugins.TapelessIngest.wrapped.resolver import (
    ResolveError,
    resolve_p2,
    wrapped_p2_clips,
)
from portal.plugins.TapelessIngest.wrapped.shape import template_disagreements
from portal.plugins.TapelessIngest.wrapped.templates import (
    common_template,
    is_proxy_copy,
    load_templates,
    signature,
    signature_difference,
    template_key,
    timing,
)
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item

_REASON_WIDTH = 120
_TOP_REASONS = 10
_MIN_REFS = 20
_MIN_SHARE = 0.95
_TEMPLATE_OPTIONS = (("out", "--out"), ("min_refs", "--min-refs"))
_TEMPLATE_OPTIONS += (("min_share", "--min-share"),)
_SHOWN_DISAGREEMENTS = 5


def _vidispine_gateway():
    # Imported lazily: it is the only module that needs a live Portal.
    from portal.plugins.TapelessIngest.wrapped.vidispine import VidispineGateway

    return VidispineGateway()


def _clip_metadata(clip):
    return dict(ClipMetadata.objects.filter(clip=clip).values_list("name", "value"))


class Command(BaseCommand):
    help = "Migrate legacy wrapped P2 items to their original files."

    gateway_factory = staticmethod(_vidispine_gateway)
    archive_factory = staticmethod(load_archive_lookup)
    disk_factory = Disk
    templates_factory = staticmethod(load_templates)

    def add_arguments(self, parser):
        parser.add_argument(
            "action", choices=("plan", "apply", "verify", "report", "templates")
        )
        parser.add_argument("--item", dest="item_id")
        parser.add_argument("--collection", dest="collection_id")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--dryrun", action="store_true")
        parser.add_argument(
            "--all",
            dest="all_rows",
            action="store_true",
            help="apply: act on every writable row (bare apply is refused)",
        )
        parser.add_argument(
            "--max-failures",
            dest="max_failures",
            type=int,
            default=20,
            help="apply: stop after this many consecutive failures (default 20)",
        )
        parser.add_argument(
            "--verdict",
            choices=verdicts.ALL,
            help="report: list 'item_id: reason' for the rows of this verdict",
        )
        parser.add_argument(
            "--delete-online-wrapped",
            dest="delete_online_wrapped",
            action="store_true",
            help="apply: delete the wrapped file of a migrated item when it is "
            "on an online legacy storage (default: keep it)",
        )
        parser.add_argument(
            "--out",
            help="templates: write the JSON here (default: print it to stdout)",
        )
        parser.add_argument(
            "--min-refs",
            dest="min_refs",
            type=int,
            help=f"templates: keep a format whose majority signature has at "
            f"least this many references (default {_MIN_REFS})",
        )
        parser.add_argument(
            "--min-share",
            dest="min_share",
            type=float,
            help=f"templates: ... and at least this share of the format's "
            f"references (default {_MIN_SHARE})",
        )

    def handle(self, *args, **options):
        if options["limit"] is not None and options["limit"] < 1:
            raise CommandError("--limit must be a positive integer")
        if options["dryrun"] and options["action"] != "apply":
            raise CommandError("--dryrun only applies to 'apply'")
        if options["delete_online_wrapped"] and options["action"] != "apply":
            raise CommandError("--delete-online-wrapped only applies to 'apply'")
        if options["max_failures"] < 1:
            raise CommandError("--max-failures must be a positive integer")
        if options["all_rows"] and options["action"] != "apply":
            raise CommandError("--all only applies to 'apply'")
        if (
            options["action"] == "apply"
            and not options["all_rows"]
            and not (options["item_id"] or options["collection_id"] or options["limit"])
        ):
            raise CommandError(
                "apply needs a scope: --item, --collection or --limit, or --all "
                "to act on every writable row"
            )
        if options["verdict"] and options["action"] != "report":
            raise CommandError("--verdict only applies to 'report'")
        for name, flag in _TEMPLATE_OPTIONS:
            if options[name] is not None and options["action"] != "templates":
                raise CommandError(f"{flag} only applies to 'templates'")
        if options["min_refs"] is not None and options["min_refs"] < 1:
            raise CommandError("--min-refs must be a positive integer")
        if options["min_share"] is not None and not 0 < options["min_share"] <= 1:
            raise CommandError("--min-share must be in (0, 1]")
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
        templates = self.templates_factory()
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
                    clip_metadata=_clip_metadata(clip),
                    templates=templates,
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
            try:
                with transaction.atomic():
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
            except Exception as error:  # noqa: BLE001 - isolate the item
                self.stdout.write(
                    f"{clip.item_id}: FAILED to save plan: "
                    f"{type(error).__name__}: {error}"
                )
                counts["save-failed"] += 1
                continue
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
        done = failed = in_a_row = 0
        for row in rows:
            used = RecordingGateway(gateway) if dry else gateway
            executor = Executor(
                used,
                disk,
                persist=not dry,
                delete_online_wrapped=options["delete_online_wrapped"],
            )
            try:
                executor.run(row)
            except Exception as error:  # noqa: BLE001 - isolate the item
                failed += 1
                in_a_row += 1
                row.error = (
                    f"after {row.phase or 'start'}: {type(error).__name__}: {error}"
                )
                if not dry:
                    row.save(update_fields=["error", "updated_on"])
                self.stdout.write(f"{row.item_id}: FAILED {row.error}")
                if in_a_row >= options["max_failures"]:
                    self.stdout.write(
                        f"stopped after {in_a_row} consecutive failures "
                        f"(--max-failures {options['max_failures']})"
                    )
                    break
                continue
            done += 1
            in_a_row = 0
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
        if options["verdict"]:
            listed = rows.filter(verdict=options["verdict"])
            if options["limit"]:
                listed = listed[: options["limit"]]
            for item_id, reason in listed.values_list("item_id", "reason"):
                self.stdout.write(f"{item_id}: {reason}")
            return
        counts = Counter((r.verdict, r.phase or "-") for r in rows)
        for (verdict, phase), count in sorted(counts.items()):
            self.stdout.write(f"{verdict}/{phase}: {count}")
        for verdict in verdicts.ALL:
            if verdict in verdicts.WRITABLE:
                continue
            reasons = Counter(
                reason[:_REASON_WIDTH]
                for reason in rows.filter(verdict=verdict).values_list(
                    "reason", flat=True
                )
            )
            if not reasons:
                continue
            self.stdout.write(f"{verdict} reasons:")
            for reason, count in reasons.most_common(_TOP_REASONS):
                self.stdout.write(f"  {count}  {reason}")
        kept = sum(
            1
            for plan in rows.values_list("plan", flat=True)
            if plan.get("wrapped_kept")
        )
        self.stdout.write(f"wrapped kept: {kept}")
        # A row planned before technical_source existed was always
        # restated from its wrapped shape (as the executor assumes).
        sources = Counter(
            plan.get("technical_source", "wrapped")
            for plan in rows.filter(verdict=verdicts.READY).values_list(
                "plan", flat=True
            )
        )
        for source, count in sorted(sources.items()):
            self.stdout.write(f"technical source {source}: {count}")
        for row in rows.exclude(error="")[:50]:
            self.stdout.write(f"ERROR {row.item_id}: {row.error}")

    def _templates(self, options):
        min_refs = options["min_refs"] or _MIN_REFS
        min_share = options["min_share"] or _MIN_SHARE
        # Genuine wrapped shapes only: a template-planned row carries a
        # proxy copy, which is exactly what a template must never learn;
        # nor does a row frozen mid-apply (its shape may be half-replaced).
        by_key = defaultdict(Counter)
        references = {}
        unkeyed = 0
        for row in self._rows(options).filter(verdict=verdicts.READY):
            plan = row.plan
            if plan.get("technical_source", "wrapped") != "wrapped":
                continue
            if row.phase or "wrapped_shape" not in plan:
                continue
            wrapped = parse_shape(plan["wrapped_shape"])
            if is_proxy_copy(wrapped):
                continue
            clip = Clip.objects.filter(umid=row.clip_umid).first()
            metadata = _clip_metadata(clip) if clip else {}
            key = template_key(metadata)
            if key is None:
                unkeyed += 1
                continue
            found = signature(wrapped)
            by_key[key][found] += 1
            references.setdefault((key, found), []).append(
                (row.item_id, plan["wrapped_shape"], metadata)
            )
        summary = self.stdout if options["out"] else self.stderr
        kept = {}
        for key in sorted(by_key):
            total = sum(by_key[key].values())
            majority, count = by_key[key].most_common(1)[0]
            share = count / total
            line = f"{key}: {total} ref(s), majority {count} ({share:.1%}): "
            if count < min_refs:
                summary.write(line + f"rejected, fewer than {min_refs} references")
                continue
            if share < min_share:
                summary.write(line + f"rejected, share below {min_share:.1%}")
                self._show_split(summary, key, by_key[key])
                continue
            majority_refs = references[(key, majority)]
            template, dropped = common_template([ref[1] for ref in majority_refs])
            names = [f"{c}.{k}" for c, keys in sorted(dropped.items()) for k in keys]
            if names:
                self.stderr.write(f"{key}: dropped per-file values {', '.join(names)}")
            disagreeing = self._round_trip(key, template, majority_refs)
            if disagreeing:
                summary.write(
                    line + f"rejected, round trip disagrees for {disagreeing} "
                    f"reference(s)"
                )
                continue
            summary.write(line + "kept")
            kept[key] = {
                "template": template,
                "reference_item": majority_refs[0][0],
                "references": count,
                "share": round(share, 4),
            }
        summary.write(f"no template key: {unkeyed}")
        text = json.dumps(kept, indent=2, sort_keys=True, ensure_ascii=True)
        if options["out"]:
            with open(options["out"], "w", encoding="ascii") as handle:
                handle.write(text + "\n")
            self.stdout.write(f"wrote {len(kept)} template(s) to {options['out']}")
        else:
            self.stdout.write(text)

    def _round_trip(self, key, template, refs) -> int:
        """How many references the template, with each one's own P2 timing,
        does not rebuild exactly; the first few are named on stderr."""
        disagreeing = 0
        for item_id, document, metadata in refs:
            try:
                differs = template_disagreements(
                    template, parse_shape(document), timing(metadata)
                )
            except ValueError as error:
                differs = [f"timing: {error}"]
            if not differs:
                continue
            disagreeing += 1
            if disagreeing <= _SHOWN_DISAGREEMENTS:
                self.stderr.write(f"{key}: {item_id} disagrees on {', '.join(differs)}")
        return disagreeing

    def _show_split(self, summary, key, signatures) -> None:
        """Why a format has no majority: its two commonest signatures, and
        the values of the fields they differ in."""
        (first, first_count), (second, second_count) = signatures.most_common(2)
        differs = signature_difference(first, second)
        summary.write(
            f"{key}:   top signatures: {first_count} vs {second_count}, "
            f"differing in {', '.join(differs)}"
        )
        ours, theirs = dict(first), dict(second)
        for name in differs:
            summary.write(
                f"{key}:     {name}: {ours.get(name)!r} ({first_count}) vs "
                f"{theirs.get(name)!r} ({second_count})"
            )
