# Operator CLI

Run from this repository on a POSIX host with Python 3.11+ and local SQLite
storage. Every command selects the shared database with `--state`. Commands print
JSON; intake and recovery summaries include scheduler calls, peak active workers,
and a bounded page of progress. The application uses only the standard library.

## Start offline intake

The shipped mock map explicitly connects decoded-message SHA-256 hashes to
saved replies under `tests/mock_responses/`. Mock mode is the default and never
reads `API_KEY`. The examples below make no live model requests.

```sh
env -u API_KEY python3 -m access_intake intake materials/requests --provider mock --state .runtime/state.sqlite
```

The default map is `config/mock-map.json`; pass `--mock-map PATH` to select
another. A directory supplies its immediate `*.json` files. Multiple input paths
are accepted; duplicate basenames in one batch are rejected before scheduling.
Unchanged terminal results, including errors, are reused. Omitting a filename
from a later intake leaves its current result intact.

To accept work without running it yet:

```sh
env -u API_KEY python3 -m access_intake intake materials/requests --state .runtime/state.sqlite --enqueue-only
env -u API_KEY python3 -m access_intake resume --state .runtime/state.sqlite --workers 4 --timeout 60 --max-calls 3
```

State and snapshot output directories are created as needed. `.runtime/` is
ignored by Git. The [README](../README.md) includes the locked setup and final
deliverables walkthrough.

## Inspect during intake

These commands can run in another terminal while workers continue:

```sh
env -u API_KEY python3 -m access_intake queue --state .runtime/state.sqlite --limit 20 --offset 0
env -u API_KEY python3 -m access_intake progress --state .runtime/state.sqlite --limit 20 --offset 0
env -u API_KEY python3 -m access_intake history --state .runtime/state.sqlite --document IT-001.json --limit 20 --offset 0
```

Pages contain `total` and `items`. Defaults are limit 100 and offset 0; the limit
can be 1–1000. Queue rows show policy status, owner, next action, provenance,
attempt, revision and source hash. History includes completed attempts in
acceptance order, including the current attempt, with extraction and settings.

Progress describes the latest scheduled attempt for each document. It includes
queued/running/retry-wait/interrupted/completed work, settings, hash, attempt,
revision, call count, next retry time and total-attempt deadline. Summary counts
include backlog and current failures. Completed peers are immediately visible.

While a revision or refresh is pending, an older completed queue row has
`stale=true` and retains its original hash/attempt. The pending source is identified
separately in progress. CLI progress labels expired running leases as interrupted
and uses the same labels in its summary counts. Stored work remains running until
recovery changes it; inspection does not perform recovery or alter stored states.

## Interrupt, resume and retry

Ctrl-C or SIGTERM stops this invocation's worker processes and expires its active
claims. Run `resume` to recover expired work and finish queued jobs from saved
inputs/settings. A forced coordinator kill can leave uncertain remote calls;
finite leases and token checks prevent stale publication when recovery runs.
Completed work is retained and is not called again by resume.

Each error queue row includes a shell-quoted `retry_command` containing the saved
attempt ID and actual state path. Run that printed command to make a new attempt
with the saved bytes and settings. Its command form is:

```text
python3 -m access_intake retry --attempt ATTEMPT_ID --state STATE_PATH
```

An unchanged intake reuses an error; explicit retry requests a new attempt.
Retries continue to work after the original input path or mock-map file changes.
Saved mock reply paths and hashes remain part of the producing configuration;
changing/deleting the reply file itself requires a refresh with a valid map.
Earlier successes remain in history when the latest attempt fails. Retrying a
superseded revision can add history but cannot overwrite a newer revision.

Default scheduling is four workers, a 60-second total attempt timeout and at most
three provider calls per attempt. Use `--workers` (1–32), `--timeout` (positive,
finite seconds), and `--max-calls` (1–3) on intake/resume/retry/refresh. Worker
limits apply per invocation; overlapping operators should size their combined
load for the shared host and provider capacity.

Transient transport/timeout/429/5xx failures use exponential backoff starting at
0.5 seconds, jitter and a 10-second cap. A valid Retry-After number or HTTP date is
honored up to 60 seconds. Backoff releases worker capacity and persists retry-wait,
call count and the original deadline across restarts. Refusal, incomplete output,
invalid JSON/schema and other terminal errors are not automatically retried.

The deadline spans all calls and backoff within one attempt. A timed-out worker
is terminated, leaving other workers available. Each worker also has an OS timer
for its remaining budget. Recovery after an uncertain crash creates a replacement
attempt and may repeat a remote call, as documented in the shared-worker design.

## Refresh settings or policy

```sh
env -u API_KEY python3 -m access_intake refresh --state .runtime/state.sqlite
env -u API_KEY python3 -m access_intake refresh --state .runtime/state.sqlite --policy-version northstar-v1 --enqueue-only
env -u API_KEY python3 -m access_intake resume --state .runtime/state.sqlite
```

Default refresh adopts the shipped prompt/schema/implementation and policy
versions while preserving each document's provider/request configuration. A
policy-version-only refresh preserves extraction settings and reroutes valid
saved facts without constructing or calling a provider. Policy versions label
the deployed rules; changing a label does not implement different office rules.
Extraction errors remain errors until explicit retry or an extraction change.

Use `--prompt-file PATH` for a changed prompt, `--mock-map PATH` for updated mock
response configuration, or `--provider live` to load explicit live environment
settings. Extraction-setting changes cause a new extraction. Producing settings
and earlier attempts remain in history. If a concurrent intake replaces a source
during refresh, refresh does not schedule its older snapshot over that source.

## Configure another saved reply

A mock-map entry has this shape; the key is the lowercase SHA-256 of the exact
decoded message encoded as UTF-8, with no whitespace normalization:

```json
{
  "MESSAGE_SHA256": {"response": "../tests/mock_responses/complete.json"}
}
```

Reply paths resolve relative to the map. Intake snapshots the selected absolute
path and file hash into settings, so resume/retry does not reread the mutable map.
Replies are bounded to 1 MiB and must use the normalized provider-envelope shape
(`status`, `refusal`, `output_text`). An absent/changed/invalid saved reply produces
an actionable mock-response error and never falls back to a live provider.

Capacity tests use an optional `substitutions` string map, such as
`{"IT-001":"IT-W0000"}`, to replay the same saved template for an authored input
with that exact ID replacement. This is an explicit mock operation; it does not
extract facts or establish live accuracy. Keep all saved test responses under
`tests/mock_responses/` and supply independently source-derived expectations.

## Export one consistent snapshot

```sh
env -u API_KEY python3 -m access_intake export --state .runtime/state.sqlite --out .runtime/snapshots
```

The command returns a manifest identifying one immutable generation containing
`extractions.jsonl` and `queue.json`. Read the pair with
`access_intake.snapshots.read_snapshot(directory)` so one resolved generation and
both checksums are used. Unreferenced generations are ignored; old generations
remain available to readers. For offline handover, pin and copy the exact
generation produced by this export:

```sh
env -u API_KEY python3 -m access_intake export --state .runtime/state.sqlite --out .runtime/snapshots --copy-to deliverables
```

Both files are checksum-verified before copying. Fixed destination filenames
are replaced individually and are for offline handover; concurrent readers
continue to use the immutable generation interface. The copied
`snapshot-manifest.json` records the generation and final byte hashes.

## Accuracy against the source ledger

The [README](../README.md) prepares a separate state containing the 20 supplied
messages and authored T27 paraphrase. Compare that state using:

```sh
env -u API_KEY python3 -m access_intake accuracy --state .runtime/accuracy.sqlite --expected tests/expected_facts.json
```

The command reads one current terminal snapshot and reports scalar, routing,
evidence and issue comparisons with observation origins/settings. Missing
records and failed extractions remain in the denominator. Scoring just the 20
supplied exports reports the missing paraphrase rather than shrinking the
252-field denominator. Duplicate/unknown documents and non-office policy
versions are rejected. Exit 0 means a comparison report was produced, even when
it lists mismatches. See [accuracy.md](../deliverables/accuracy.md) for expected
facts, source quotations and counting rules; inspect pending work separately
with progress before treating a measurement as complete.

## Live mode and verification limits

Live mode requires an explicit `--provider live` at intake/refresh, an environment
`API_KEY`, `MODEL_ENDPOINT` and `MODEL_ID`. The full supported contract and optional
variables are in [provider.md](provider.md). Resume/retry reconstruct the saved
provider configuration and load its environment credential afresh. Mock commands
and tests never contact a live provider. No live run is claimed here.

Run the offline test suite:

```sh
env -u API_KEY python3 -m unittest discover -s tests -v
```

T39/T40 exercise guarded fresh CLI interpreters, a held peer, interrupted-work
recovery, all six route/error outcomes, persisted retry-wait, capped calls and
backoff, hard deadlines and 400/2,000-message mock batches. The workload checks
assert source hashes, source-ledger facts, uniqueness, pagination, actual call
counts and actual peak concurrency. They establish capacity behavior under mocks;
they do not establish a production latency SLA, quotas, cost or live accuracy.

Exit 0 means the command ran, including batches containing per-document errors;
inspect failure counts and queue rows. Invalid configuration/arguments or an
unavailable state database return exit 2. An interrupted running CLI returns 130.
