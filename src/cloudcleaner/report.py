"""Human- and machine-readable reports for a scan.

The text report is what gets shown to the customer: how much of their
bucket is dead weight, and what it costs them every month to keep it.
"""

from __future__ import annotations

import json

from cloudcleaner.models import ScanResult
from cloudcleaner.pricing import PricingModel, Savings


def human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def compute_savings(result: ScanResult, pricing: PricingModel) -> Savings:
    return pricing.estimate_savings(result.scanned_by_class, result.candidates_by_class)


def text_report(result: ScanResult, savings: Savings, retention_days: int, limit: int = 20) -> str:
    by_rule: dict[str, tuple[int, int]] = {}
    for cand in result.candidates:
        count, size = by_rule.get(cand.rule_name, (0, 0))
        by_rule[cand.rule_name] = (count + 1, size + cand.obj.size_bytes)

    cur = savings.currency
    lines = [
        f"Bucket: {result.bucket}",
        f"Scanned: {result.scanned_count} objects, {human_size(result.scanned_bytes)}",
        f"Cleanup candidates: {result.candidate_count} objects, "
        f"{human_size(result.candidate_bytes)}",
        "",
        f"Current storage cost:   {savings.current_monthly:10.2f} {cur}/month",
        f"Cost after cleanup:     {savings.after_monthly:10.2f} {cur}/month",
        f"Estimated savings:      {savings.monthly:10.2f} {cur}/month "
        f"({savings.yearly:.2f} {cur}/year)",
        "",
    ]
    if by_rule:
        lines.append("Matches per rule:")
        for name, (count, size) in sorted(by_rule.items(), key=lambda i: -i[1][1]):
            lines.append(f"  - {name}: {count} objects, {human_size(size)}")
        lines.append("")
        shown = result.candidates[:limit]
        lines.append(f"Candidates (first {len(shown)} of {result.candidate_count}):")
        for cand in shown:
            obj = cand.obj
            lines.append(
                f"  {obj.key}  [{human_size(obj.size_bytes)}, "
                f"{obj.last_modified.date()}, rule: {cand.rule_name}]"
            )
        lines.append("")
        lines.append(
            f"Nothing has been deleted. Run `cloudcleaner quarantine --apply` to move "
            f"these objects to quarantine for {retention_days} days before final deletion."
        )
    else:
        lines.append("No objects matched the cleanup rules.")
    return "\n".join(lines)


def json_report(result: ScanResult, savings: Savings) -> str:
    return json.dumps(
        {
            "bucket": result.bucket,
            "scanned": {
                "objects": result.scanned_count,
                "bytes": result.scanned_bytes,
                "bytes_by_storage_class": result.scanned_by_class,
            },
            "candidates": {
                "objects": result.candidate_count,
                "bytes": result.candidate_bytes,
                "bytes_by_storage_class": result.candidates_by_class,
                "items": [
                    {
                        "key": c.obj.key,
                        "size_bytes": c.obj.size_bytes,
                        "last_modified": c.obj.last_modified.isoformat(),
                        "storage_class": c.obj.storage_class,
                        "rule": c.rule_name,
                    }
                    for c in result.candidates
                ],
            },
            "savings": {
                "currency": savings.currency,
                "current_monthly": round(savings.current_monthly, 4),
                "after_monthly": round(savings.after_monthly, 4),
                "monthly": round(savings.monthly, 4),
                "yearly": round(savings.yearly, 4),
            },
        },
        indent=2,
    )
