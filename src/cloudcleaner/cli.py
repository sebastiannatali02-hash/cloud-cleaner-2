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
from cloudcleaner.quarantine import QuarantineManager
from cloudcleaner.report import compute_savings, human_size, json_report, text_report
from cloudcleaner.rules import RuleEngine


def _load(args) -> Config:
    config = load_config(args.config)
    if getattr(args, "bucket", None):
        config.bucket = args.bucket
    return config


def _scan(config: Config, adapter):
    engine = RuleEngine(config)
    return engine.scan(adapter.list_objects(config.bucket, prefix=config.prefix))


def cmd_scan(args) -> int:
    config = _load(args)
    adapter = get_adapter(config)
    result = _scan(config, adapter)
    savings = compute_savings(result, config.pricing)
    output = (
        json_report(result, savings)
        if args.json
        else text_report(result, savings, config.quarantine.retention_days, limit=args.limit)
    )
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
    p.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    p.add_argument("--output", "-o", help="write the report to a file instead of stdout")
    p.add_argument("--limit", type=int, default=20, help="max candidates shown in text report")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("quarantine", help="move matching objects into quarantine")
    add_common(p)
    p.add_argument("--apply", action="store_true", help="actually move objects (default: dry run)")
    p.add_argument("--limit", type=int, default=20, help="max candidates shown in dry run")
    p.set_defaults(func=cmd_quarantine)

    p = sub.add_parser("batches", help="list quarantine batches and their expiry")
    add_common(p)
    p.set_defaults(func=cmd_batches)

    p = sub.add_parser("purge", help="permanently delete expired quarantine batches")
    add_common(p)
    p.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    p.add_argument("--force", action="store_true", help="include batches still in retention")
    p.add_argument("--batch", help="limit the purge to one batch id")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("restore", help="bring quarantined objects back to their original keys")
    add_common(p)
    p.add_argument("--batch", required=True, help="quarantine batch id to restore from")
    p.add_argument("--key", action="append", help="restore only this original key (repeatable)")
    p.set_defaults(func=cmd_restore)

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
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
