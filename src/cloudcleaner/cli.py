"""cloudcleaner command line interface.

Every destructive command is a dry-run unless ``--apply`` is passed,
and even then objects only move to quarantine first; permanent
deletion happens through ``purge`` after the retention window.
"""

from __future__ import annotations

import argparse
import sys

from cloudcleaner.adapters import get_adapter
from cloudcleaner.config import Config, ConfigError, load_config
from cloudcleaner.quarantine import ManifestIntegrityError, QuarantineManager
from cloudcleaner.report import (
    compute_savings,
    csv_report,
    html_report,
    human_size,
    json_report,
    text_report,
)
from cloudcleaner.rules import RuleEngine


def _load(args) -> Config:
    config = load_config(args.config)
    if getattr(args, "bucket", None):
        config.bucket = args.bucket
    return config


def _scan(config: Config, adapter, now=None):
    engine = RuleEngine(config, now=now)
    return engine.scan(adapter.list_objects(config.bucket, prefix=config.prefix))


def cmd_scan(args) -> int:
    config = _load(args)
    adapter = get_adapter(config)
    now = None
    if getattr(args, "as_of", None):
        from datetime import datetime, timezone

        from cloudcleaner.config import parse_cutoff

        now = parse_cutoff(args.as_of, datetime.now(timezone.utc))
    result = _scan(config, adapter, now=now)
    savings = compute_savings(result, config.pricing)
    # --json is a deprecated alias for --format json; honor it when set.
    fmt = "json" if getattr(args, "json", False) else args.format
    retention = config.quarantine.retention_days
    if fmt == "json":
        output = json_report(result, savings)
    elif fmt == "html":
        output = html_report(result, savings, retention)
    elif fmt == "csv":
        output = csv_report(result, savings)
    else:
        output = text_report(result, savings, retention, limit=args.limit)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output + "\n")
        print(f"Report written to {args.output}")
    else:
        print(output)
    return 0


def cmd_quarantine(args) -> int:
    config = _load(args)
    adapter = get_adapter(config)
    result = _scan(config, adapter)
    if not result.candidates:
        print("No objects matched the cleanup rules; nothing to quarantine.")
        return 0

    if not args.apply:
        savings = compute_savings(result, config.pricing)
        print(text_report(result, savings, config.quarantine.retention_days, limit=args.limit))
        print("\nDry run: no changes made. Re-run with --apply to quarantine these objects.")
        return 0

    # Blast-radius guardrail: refuse an --apply that would quarantine an
    # unexpectedly large share of the bucket, unless --force overrides.
    if not args.force:
        from cloudcleaner.guardrail import (
            GuardrailLimits,
            GuardrailViolation,
            check_guardrail,
        )

        try:
            check_guardrail(result, GuardrailLimits.from_dict(config.quarantine.guardrail))
        except GuardrailViolation as exc:
            print(f"guardrail: {exc}", file=sys.stderr)
            return 1

    manager = QuarantineManager(adapter, config)
    manifest = manager.quarantine(result.candidates)
    print(
        f"Quarantined {len(manifest.entries)} objects "
        f"({human_size(manifest.total_bytes)}) as batch {manifest.batch_id}."
    )
    print(f"They will become purgeable after {manifest.purge_after}.")
    print(f"Restore with: cloudcleaner restore --config {args.config} --batch {manifest.batch_id}")
    return 0


def cmd_batches(args) -> int:
    config = _load(args)
    manager = QuarantineManager(get_adapter(config), config)
    batches = manager.list_batches()
    if not batches:
        print("No quarantine batches found.")
        return 0
    for m in batches:
        state = "EXPIRED (purgeable)" if m.is_expired(manager.now) else f"held until {m.purge_after}"
        print(f"{m.batch_id}: {len(m.entries)} objects, {human_size(m.total_bytes)} — {state}")
    return 0


def cmd_purge(args) -> int:
    config = _load(args)
    manager = QuarantineManager(get_adapter(config), config)
    batches = manager.list_batches() if args.force else manager.expired_batches()
    if args.batch:
        batches = [m for m in batches if m.batch_id == args.batch]
    if not batches:
        print("No purgeable quarantine batches (retention window still open).")
        return 0

    total = sum(m.total_bytes for m in batches)
    for m in batches:
        print(f"batch {m.batch_id}: {len(m.entries)} objects, {human_size(m.total_bytes)}")
    if not args.apply:
        print(
            f"\nDry run: would permanently delete {human_size(total)} across "
            f"{len(batches)} batch(es). Re-run with --apply to proceed."
        )
        return 0

    # About to permanently delete: show every key and require confirmation.
    all_keys = [e.original_key for m in batches for e in m.entries]
    print(f"\nThe following {len(all_keys)} object(s) will be PERMANENTLY deleted:")
    for key in all_keys:
        print(f"  {key}")

    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "\nRefusing to purge without confirmation in a non-interactive session. "
                "Re-run with --yes to confirm permanent deletion.",
                file=sys.stderr,
            )
            return 1
        answer = input(
            f"\nType 'yes' to permanently delete {human_size(total)} across "
            f"{len(batches)} batch(es): "
        )
        if answer.strip().lower() != "yes":
            print("Aborted; nothing was deleted.")
            return 1

    freed = manager.purge(batches)
    print(f"Permanently deleted {human_size(freed)} across {len(batches)} batch(es).")
    return 0


def cmd_restore(args) -> int:
    config = _load(args)
    manager = QuarantineManager(get_adapter(config), config)
    try:
        restored = manager.restore(args.batch, keys=args.key or None)
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 1
    print(f"Restored {len(restored)} objects from batch {args.batch}:")
    for entry in restored:
        print(f"  {entry.original_key}")
    return 0


def cmd_dedup(args) -> int:
    from cloudcleaner.dedup import choose_redundant, find_duplicates, total_reclaimable
    from cloudcleaner.rules import ExclusionPolicy

    config = _load(args)
    adapter = get_adapter(config)
    # Honor exclusions (e.g. legal-hold/) and never touch the quarantine
    # prefix — dedup must respect the same protections as the rule engine.
    exclusions = ExclusionPolicy(config.exclude, config.quarantine.prefix)
    objects = (
        o
        for o in adapter.list_objects(config.bucket, prefix=config.prefix)
        if not exclusions.is_excluded(o.key)
    )
    groups = find_duplicates(objects, min_size=args.min_size)
    if not groups:
        print("No exact-duplicate objects found (by ETag + size).")
        return 0

    redundant_total = 0
    for g in groups:
        redundant = choose_redundant(g, keep=args.keep)
        redundant_total += len(redundant)
        print(
            f"{g.count} copies, {human_size(g.size_bytes)} each "
            f"(reclaimable {human_size(g.redundant_bytes)}) — etag {g.etag}:"
        )
        kept = [m for m in g.members if m not in redundant]
        for m in kept:
            print(f"  keep    {m.key}")
        for m in redundant:
            print(f"  redundant {m.key}")
    print(
        f"\n{len(groups)} duplicate group(s), {redundant_total} redundant object(s), "
        f"{human_size(total_reclaimable(groups))} reclaimable (keeping one per group)."
    )
    return 0


def cmd_multipart(args) -> int:
    from cloudcleaner.multipart import abort_uploads, find_incomplete_uploads

    config = _load(args)
    adapter = get_adapter(config)
    uploads = find_incomplete_uploads(adapter, config.bucket, older_than=args.older_than)
    if not uploads:
        print("No incomplete multipart uploads found.")
        return 0

    for u in uploads:
        size = human_size(u.size_bytes) if u.size_bytes is not None else "unknown size"
        print(f"  {u.key}  [upload {u.upload_id}, initiated {u.initiated}, {size}]")

    total = sum(u.size_bytes or 0 for u in uploads)
    if not args.apply:
        print(
            f"\nDry run: would abort {len(uploads)} incomplete upload(s), "
            f"reclaiming ~{human_size(total)}. Re-run with --apply to abort them."
        )
        return 0

    count, reclaimed = abort_uploads(adapter, config.bucket, uploads)
    print(f"Aborted {count} incomplete upload(s), reclaimed ~{human_size(reclaimed)}.")
    return 0


def cmd_demo(args) -> int:
    from cloudcleaner.demo import run_demo

    run_demo()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudcleaner",
        description=(
            "Rule-based cloud storage cleanup: scan a bucket, estimate savings, "
            "quarantine stale objects, then purge them after a retention window."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, bucket=True):
        p.add_argument("--config", "-c", required=True, help="path to the YAML config")
        if bucket:
            p.add_argument("--bucket", help="override the bucket from the config")

    p = sub.add_parser("scan", help="dry-run: list candidates and estimated savings")
    add_common(p)
    p.add_argument(
        "--format",
        choices=["text", "json", "html", "csv"],
        default="text",
        help="report format (default: text)",
    )
    # Deprecated alias for --format json, kept for backward compatibility.
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--output", "-o", help="write the report to a file instead of stdout")
    p.add_argument("--limit", type=int, default=20, help="max candidates shown in text report")
    p.add_argument(
        "--as-of",
        dest="as_of",
        help="preview candidates as if it were this ISO date (e.g. 2027-01-01)",
    )
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("quarantine", help="move matching objects into quarantine")
    add_common(p)
    p.add_argument("--apply", action="store_true", help="actually move objects (default: dry run)")
    p.add_argument("--limit", type=int, default=20, help="max candidates shown in dry run")
    p.add_argument(
        "--force",
        action="store_true",
        help="override the blast-radius guardrail (quarantine even a large share of the bucket)",
    )
    p.set_defaults(func=cmd_quarantine)

    p = sub.add_parser("batches", help="list quarantine batches and their expiry")
    add_common(p)
    p.set_defaults(func=cmd_batches)

    p = sub.add_parser("purge", help="permanently delete expired quarantine batches")
    add_common(p)
    p.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    p.add_argument("--force", action="store_true", help="include batches still in retention")
    p.add_argument("--batch", help="limit the purge to one batch id")
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the interactive confirmation prompt before permanent deletion",
    )
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("restore", help="bring quarantined objects back to their original keys")
    add_common(p)
    p.add_argument("--batch", required=True, help="quarantine batch id to restore from")
    p.add_argument("--key", action="append", help="restore only this original key (repeatable)")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("dedup", help="find exact-duplicate objects (by ETag + size)")
    add_common(p)
    p.add_argument(
        "--min-size",
        dest="min_size",
        type=int,
        default=0,
        help="ignore objects smaller than this many bytes",
    )
    p.add_argument(
        "--keep",
        choices=["oldest", "newest"],
        default="oldest",
        help="which copy in each duplicate group to keep (default: oldest)",
    )
    p.set_defaults(func=cmd_dedup)

    p = sub.add_parser("multipart", help="find/abort incomplete multipart uploads")
    add_common(p)
    p.add_argument(
        "--older-than",
        dest="older_than",
        help="only uploads initiated before this age/date (e.g. 7d, 2027-01-01)",
    )
    p.add_argument(
        "--apply", action="store_true", help="actually abort uploads (default: dry run)"
    )
    p.set_defaults(func=cmd_multipart)

    p = sub.add_parser("demo", help="run an offline demo on a simulated bucket")
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ManifestIntegrityError as exc:
        print(f"integrity error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
