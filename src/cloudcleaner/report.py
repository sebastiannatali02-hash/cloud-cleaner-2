"""Human- and machine-readable reports for a scan.

The text report is what gets shown to the customer: how much of their
bucket is dead weight, and what it costs them every month to keep it.
"""

from __future__ import annotations

import csv
import html
import io
import json

from cloudcleaner.models import ScanResult
from cloudcleaner.pricing import PricingModel, Savings


def _by_rule(result: ScanResult) -> dict[str, tuple[int, int]]:
    """Aggregate candidate count and total bytes per matching rule."""
    totals: dict[str, tuple[int, int]] = {}
    for cand in result.candidates:
        count, size = totals.get(cand.rule_name, (0, 0))
        totals[cand.rule_name] = (count + 1, size + cand.obj.size_bytes)
    return totals


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
    by_rule = _by_rule(result)

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


_HTML_STYLE = """
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: #1a2b3c;
      background: #f4f6f9;
      line-height: 1.5;
    }
    .wrap { max-width: 960px; margin: 0 auto; padding: 40px 24px 64px; }
    header { margin-bottom: 32px; }
    .brand { font-size: 14px; letter-spacing: .12em; text-transform: uppercase; color: #6b7c93; margin: 0 0 4px; }
    h1 { font-size: 26px; margin: 0; }
    .sub { color: #6b7c93; margin: 4px 0 0; }
    .hero {
      margin: 28px 0;
      padding: 32px;
      border-radius: 14px;
      background: linear-gradient(135deg, #0f766e 0%, #115e59 100%);
      color: #fff;
      box-shadow: 0 8px 24px rgba(15, 118, 110, .25);
    }
    .hero .label { text-transform: uppercase; letter-spacing: .1em; font-size: 13px; opacity: .85; margin: 0; }
    .hero .figure { font-size: 46px; font-weight: 700; margin: 6px 0 2px; }
    .hero .year { font-size: 16px; opacity: .9; margin: 0; }
    .cards { display: flex; flex-wrap: wrap; gap: 16px; margin: 24px 0; }
    .card { flex: 1 1 160px; background: #fff; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    .card .k { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: #6b7c93; margin: 0 0 6px; }
    .card .v { font-size: 22px; font-weight: 600; margin: 0; }
    h2 { font-size: 18px; margin: 36px 0 12px; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    th, td { text-align: left; padding: 12px 16px; border-bottom: 1px solid #eef1f5; font-size: 14px; }
    th { background: #f8fafc; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: #6b7c93; }
    tbody tr:last-child td { border-bottom: none; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
    .key { font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; font-size: 13px; word-break: break-all; }
    .note { margin-top: 28px; padding: 16px 20px; background: #fff7ed; border-left: 4px solid #f59e0b; border-radius: 8px; color: #7c2d12; font-size: 14px; }
    footer { margin-top: 40px; color: #9aa7b8; font-size: 12px; text-align: center; }
""".rstrip()


def html_report(result: ScanResult, savings: Savings, retention_days: int) -> str:
    """A self-contained, styled HTML savings report suitable for sharing.

    All dynamic strings are escaped with ``html.escape``. Inline CSS only,
    so the page renders identically when emailed or saved to disk.
    """
    e = html.escape
    cur = e(savings.currency)
    bucket = e(result.bucket)
    by_rule = _by_rule(result)

    if by_rule:
        rule_rows = "\n".join(
            "        <tr>"
            f"<td>{e(name)}</td>"
            f'<td class="num">{count}</td>'
            f'<td class="num">{e(human_size(size))}</td>'
            "</tr>"
            for name, (count, size) in sorted(by_rule.items(), key=lambda i: -i[1][1])
        )
        rule_table = (
            "      <h2>Savings by rule</h2>\n"
            "      <table>\n"
            "        <thead><tr><th>Rule</th>"
            '<th class="num">Objects</th><th class="num">Reclaimable</th></tr></thead>\n'
            f"        <tbody>\n{rule_rows}\n        </tbody>\n"
            "      </table>"
        )

        cand_rows = "\n".join(
            "        <tr>"
            f'<td class="key">{e(c.obj.key)}</td>'
            f'<td class="num">{e(human_size(c.obj.size_bytes))}</td>'
            f"<td>{e(c.obj.last_modified.date().isoformat())}</td>"
            f"<td>{e(c.obj.storage_class)}</td>"
            f"<td>{e(c.rule_name)}</td>"
            "</tr>"
            for c in result.candidates
        )
        cand_table = (
            f"      <h2>Cleanup candidates ({result.candidate_count})</h2>\n"
            "      <table>\n"
            "        <thead><tr><th>Key</th>"
            '<th class="num">Size</th><th>Last modified</th>'
            "<th>Storage class</th><th>Rule</th></tr></thead>\n"
            f"        <tbody>\n{cand_rows}\n        </tbody>\n"
            "      </table>"
        )

        note = (
            '      <div class="note">Nothing has been deleted. These objects would move to '
            f"quarantine for {retention_days} days before any permanent deletion.</div>"
        )
        body_sections = f"{rule_table}\n{cand_table}\n{note}"
    else:
        body_sections = (
            '      <div class="note">No objects matched the cleanup rules — '
            "this bucket is already lean.</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cloudcleaner savings report — {bucket}</title>
<style>{_HTML_STYLE}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <p class="brand">cloudcleaner</p>
      <h1>Storage savings report</h1>
      <p class="sub">Bucket <strong>{bucket}</strong></p>
    </header>

    <section class="hero">
      <p class="label">Estimated monthly savings</p>
      <p class="figure">{savings.monthly:,.2f} {cur}</p>
      <p class="year">{savings.yearly:,.2f} {cur} per year</p>
    </section>

    <section class="cards">
      <div class="card"><p class="k">Objects scanned</p><p class="v">{result.scanned_count:,}</p></div>
      <div class="card"><p class="k">Data scanned</p><p class="v">{e(human_size(result.scanned_bytes))}</p></div>
      <div class="card"><p class="k">Cleanup candidates</p><p class="v">{result.candidate_count:,}</p></div>
      <div class="card"><p class="k">Reclaimable</p><p class="v">{e(human_size(result.candidate_bytes))}</p></div>
    </section>

    <section class="cards">
      <div class="card"><p class="k">Current cost</p><p class="v">{savings.current_monthly:,.2f} {cur}/mo</p></div>
      <div class="card"><p class="k">After cleanup</p><p class="v">{savings.after_monthly:,.2f} {cur}/mo</p></div>
    </section>

{body_sections}

    <footer>Generated by cloudcleaner. Estimates based on configured storage pricing.</footer>
  </div>
</body>
</html>
"""


def csv_report(result: ScanResult, savings: Savings) -> str:
    """CSV of candidate objects plus a summary block, as a single string."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["key", "size_bytes", "last_modified", "storage_class", "rule"])
    for c in result.candidates:
        writer.writerow(
            [
                c.obj.key,
                c.obj.size_bytes,
                c.obj.last_modified.isoformat(),
                c.obj.storage_class,
                c.rule_name,
            ]
        )

    writer.writerow([])
    writer.writerow(["summary", "value"])
    writer.writerow(["bucket", result.bucket])
    writer.writerow(["scanned_objects", result.scanned_count])
    writer.writerow(["scanned_bytes", result.scanned_bytes])
    writer.writerow(["candidate_objects", result.candidate_count])
    writer.writerow(["candidate_bytes", result.candidate_bytes])
    writer.writerow(["currency", savings.currency])
    writer.writerow(["current_monthly", round(savings.current_monthly, 4)])
    writer.writerow(["after_monthly", round(savings.after_monthly, 4)])
    writer.writerow(["monthly_savings", round(savings.monthly, 4)])
    writer.writerow(["yearly_savings", round(savings.yearly, 4)])

    return buf.getvalue()
