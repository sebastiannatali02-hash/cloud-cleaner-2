# cloudcleaner — review guide

This branch (`feature/scale-safety-multicloud-reporting`) adds a large batch of
capability plus a local test UI on top of the original CLI tool. This document
is written for a product review and deliberately covers **both** framings:
a capability/demo walkthrough and an honest ship-readiness assessment.

## What cloudcleaner is

Rule-based cloud storage cleanup with cost reporting. Point it at a bucket,
describe what counts as dead weight, and it reports how much that data costs
per month, then removes it **safely** — dry-run by default, quarantine before
delete, restorable, with a retention window before permanent purge.

The savings report is the product: cost before vs. after cleanup.

## Demo walkthrough (≈5 min)

Run the local console:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python ui/server.py         # http://127.0.0.1:8765
```

1. **Load large dataset (~66 GiB)** — builds real data server-side so the
   dollar savings are non-trivial (~$1.38/mo · $16.56/yr on 60 GiB of
   candidates). *(Real S3 storage; Reset when done.)*
2. **Overview + bucket visualization** — KPI tiles and a per-prefix bar
   (live / cleanup-candidate / protected / quarantined).
3. **Build a cleanup run** — tick which cleans to include (dedup, multipart,
   quarantine, purge, restore), configure each, choose Dry run vs Apply, run
   them as one batch with a **live progress bar**.
4. **Safety story** — `legal-hold/` is never touched; quarantine is
   restorable; purge needs confirmation and honors a retention window; a
   blast-radius guardrail refuses over-broad runs.

## Capabilities added on this branch

- **Rules**: boolean composition (`any_of`/`all_of`/`none_of`) + `keep_newest`
  N-per-group (e.g. keep the 3 newest backups).
- **Cleanups**: quarantine→purge lifecycle, exact-duplicate detection (by
  ETag), incomplete-multipart-upload cleanup.
- **Safety**: manifest SHA-256 integrity, append-only audit log, purge
  confirmation, blast-radius guardrail, crash-safe/idempotent restore.
- **Scale/perf**: bulk delete, parallelized quarantine + restore, real-time
  progress.
- **Concurrency**: distributed bucket lock (S3 conditional writes) enforced
  across UI **and** CLI so concurrent runs / a second host can't race.
- **Reporting**: text / JSON / HTML / CSV.
- **Multi-cloud**: GCS + Azure adapters (see caveat below).
- **UI**: local test console driving the real code paths.

Test suite: **151 tests passing.** Every feature has also been exercised
end-to-end against real S3.

## Known gaps — read before a ship decision

These are deliberate limits, surfaced rather than hidden:

1. **The UI is a test harness, not a product surface.** It binds to
   `127.0.0.1`, hardcodes one bucket, and has **no authN/authZ**. Anyone who
   can reach the port can delete data. It is for demos/testing, not customers.
2. **Cost model = AWS us-east-1 list prices.** No negotiated rates, limited
   region coverage. Since the savings number *is* the product, this needs work
   before it's a trustworthy sales/billing input.
3. **Scale ceiling ≈ low millions of objects.** Candidate lists, dedup
   grouping, and the manifest are held in memory; the audit log is a single
   read-modify-write object. Fine for hundreds of GB / low-millions of
   objects; not proven at true TB / tens-of-millions.
4. **Dedup is blind to large (multipart-uploaded) objects** — their ETags
   aren't content MD5s, so they're skipped by design. Dedup is effectively a
   small/medium-object feature.
5. **GCS + Azure adapters are unit-tested with mocks only** — never run
   against real GCS/Azure. Treat as unproven.
6. **The bucket lock is advisory** — it protects callers that use cloudcleaner;
   a raw `aws s3` command bypasses it (true of any such lock).

## Suggested next steps (if this proceeds toward ship)

- Trustworthy cost model (region/negotiated pricing).
- S3 Lifecycle-policy export — the scalable path for age-based cleanup (let AWS
  expire/transition server-side instead of copying object-by-object).
- Streaming scan + sharded audit log to lift the scale ceiling.
- Real integration tests for GCS/Azure.
- A real product UI with auth, if the console graduates from demo to surface.
