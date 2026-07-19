# cloudcleaner

Rule-based cloud storage cleanup. Point it at a bucket, describe what
counts as dead weight (keywords, retention/prescription periods, size,
prefixes), and it tells you exactly how much money that data costs every
month — then removes it safely through a quarantine, never with a direct
delete.

The savings report is the product: it computes the storage bill before
and after the cleanup, which is the number the pricing model is built on
(a share of the difference between what the customer spends today and
what they spend after the cleanup).

## How it works

```
scan  ──►  quarantine --apply  ──►  purge --apply
(dry run,      (objects moved,          (permanent delete,
 savings        restorable for           only after the
 report)        N days)                  retention window)
```

1. **`scan`** never touches anything. It lists the bucket, applies the
   rules and prints the candidates plus the before/after monthly cost.
2. **`quarantine --apply`** *moves* the candidates under a quarantine
   prefix and writes a JSON manifest. Nothing is lost yet: `restore`
   puts any object back exactly where it was.
3. **`purge --apply`** permanently deletes quarantined batches, but only
   once their retention window (default 30 days) has expired.

Every mutating command is a dry run unless `--apply` is passed.

## Install

```bash
pip install -e .          # requires Python 3.10+
cloudcleaner demo         # offline demo on a simulated bucket, no credentials needed
```

AWS credentials are picked up from the standard chain (environment,
`~/.aws/credentials`, instance profile). S3-compatible storage (MinIO,
Cloudflare R2) works via `endpoint_url` in the config.

## Configuration

Everything is driven by one YAML file — see
[`examples/rules.example.yaml`](examples/rules.example.yaml) for the
full annotated version:

```yaml
provider: s3
bucket: acme-corp-data

rules:
  - name: stale-logs
    keywords: [log]
    older_than: 90d

  # legal retention (prescription) period
  - name: invoices-past-retention
    prefixes: [invoices/]
    older_than: 10y

  - name: big-old-backups
    prefixes: [backups/]
    older_than: 1y
    min_size: 1GB

exclude:
  - legal-hold/        # never touched, whatever the rules say

quarantine:
  retention_days: 30
```

An object is a candidate when **all** conditions of at least one rule
hold. Available conditions: `keywords`, `match_regex`, `prefixes`,
`suffixes`, `older_than` (ISO date or `90d`/`6m`/`10y`), `min_size`,
`storage_classes`. `exclude` patterns always win over rules.

Matching runs on object keys and metadata only (name, path, age, size,
storage class) — objects are never downloaded, so a scan costs almost
nothing even on large buckets.

## Commands

```bash
cloudcleaner scan       -c rules.yaml               # candidates + savings report
cloudcleaner scan       -c rules.yaml --json -o report.json
cloudcleaner quarantine -c rules.yaml               # dry run
cloudcleaner quarantine -c rules.yaml --apply       # move to quarantine
cloudcleaner batches    -c rules.yaml               # list quarantine batches
cloudcleaner restore    -c rules.yaml --batch <ID>  # undo (whole batch or --key ...)
cloudcleaner purge      -c rules.yaml --apply       # delete expired batches for good
cloudcleaner demo                                   # simulated bucket, offline
```

## Cost model

Savings are estimated from per-storage-class volumes using public AWS
S3 us-east-1 list prices (Standard is tiered by volume; IA/Glacier/Deep
Archive are flat). Override any price per GB-month in the config to
match a different region or negotiated rates:

```yaml
pricing:
  currency: EUR
  overrides:
    STANDARD: 0.024
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The core only speaks the `StorageAdapter` interface
(`src/cloudcleaner/adapters/`); adding Azure Blob or GCS means
implementing that interface and registering a factory with the
`@register_adapter("name")` decorator — no existing code changes. The
`memory` provider is a full in-process implementation used by the test
suite and the demo.
