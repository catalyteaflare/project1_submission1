# IT access-request intake

Prepare a human review queue from exported messages. Validated structured facts
are routed under the Northstar office policy, with durable attempts and source
revisions in SQLite. The application never grants access or marks approval.

Use **Python 3.11+ on a POSIX host**, with SQLite, `venv` and host pip 22.3+ available.
There are no third-party dependencies; `requirements.lock` records that empty
dependency closure. The host supplies Python and SQLite. Run from the repository
root; `access_intake` runs directly without a build step.

## Offline walkthrough

Create a clean environment with a locked offline install, process the 20 supplied
messages, inspect work, and copy one verified snapshot pair to `deliverables/`.
A separate 21-case accuracy state includes the authored paraphrase; the final
handover pair stays at 20. Repeating intake reuses unchanged terminal records.
Choose a new state path for a new independent observation. `.runtime/` is ignored.

<!-- offline-walkthrough:start -->
```sh
python3 -m venv --without-pip .venv
python3 -m pip --python .venv/bin/python install --no-index --require-hashes -r requirements.lock
mkdir -p .runtime
env -u API_KEY .venv/bin/python -m access_intake intake materials/requests --provider mock --state .runtime/handover.sqlite --workers 4 --timeout 60 --max-calls 3 > .runtime/intake.json
env -u API_KEY .venv/bin/python -m access_intake queue --state .runtime/handover.sqlite --limit 20 --offset 0 > .runtime/queue-page.json
env -u API_KEY .venv/bin/python -m access_intake progress --state .runtime/handover.sqlite --limit 20 --offset 0 > .runtime/progress.json
env -u API_KEY .venv/bin/python -m access_intake history --state .runtime/handover.sqlite --document IT-001.json --limit 20 > .runtime/history.json
env -u API_KEY .venv/bin/python -m access_intake export --state .runtime/handover.sqlite --out .runtime/snapshots --copy-to deliverables
env -u API_KEY .venv/bin/python -m tools.prepare_accuracy_inputs
env -u API_KEY .venv/bin/python -m access_intake intake .runtime/ledger-inputs --provider mock --state .runtime/accuracy.sqlite --workers 4 --timeout 60 --max-calls 3 > .runtime/accuracy-intake.json
env -u API_KEY .venv/bin/python -m access_intake accuracy --state .runtime/accuracy.sqlite --expected tests/expected_facts.json > .runtime/accuracy.json
```
<!-- offline-walkthrough:end -->

Read `.runtime/queue-page.json`, `.runtime/progress.json`, `.runtime/history.json`
and `.runtime/accuracy.json`. Run inspection commands without redirection to see
JSON while another operator continues processing. Pages contain `items` and
`total`. Progress shows pending work separately from policy status; prior queue
rows are marked stale while a newer revision is pending.

See [the operator guide](docs/operator.md) for pagination, refresh, enqueue-only
intake, saved-attempt retry commands, interruption/resume and scheduler bounds.
`export --copy-to` verifies and pins the generation it just exported, copying its
exact bytes and `snapshot-manifest.json`. Fixed handover filenames are replaced
individually. Concurrent application readers use `read_snapshot` with immutable
generations and a single manifest resolution, rather than fixed handover files.

## Tests and evidence

```sh
env -u API_KEY python3 -m unittest discover -s tests
env -u API_KEY python3 -m tools.record_request_evidence
```

Tests use saved responses in `tests/mock_responses/`, temporary state, fake HTTP
transports, key guards and network denial. T41 creates a fresh environment and
executes the walkthrough; it does not recursively execute the suite. Adapter
tests inject dummy credentials in memory. The evidence command uses the actual
adapter with a saved HTTP response and a fake transport, redacts Authorization,
and never reads a real credential.

[Accuracy](deliverables/accuracy.md) publishes the frozen source-derived oracle
and comparisons. The accuracy JSON reports per-field/per-document counts,
factual errors with source quotations, separate routing/evidence/issue checks,
and observation origin/settings. All 21 cases remain in the 252-scalar
denominator, including missing/failed extractions. Purpose wording compares
literally; factual equivalence needs separate human assessment. Mock agreement
does not measure live model quality. [Verification](deliverables/verification.md)
records commands, assertions, actual observations and operating limits.

## Provider and configuration

Mock is the default. `config/mock-map.json` explicitly maps decoded-message hashes
to saved envelopes for the 20 exports and T27 paraphrase. Mock mode never reads
`API_KEY` and has no live fallback. New messages need a configured saved response
or an explicitly selected live run.

Live construction requires `--provider live` and environment variables
`API_KEY`, `MODEL_ENDPOINT`, and `MODEL_ID`; `MODEL_PROVIDER` labels provenance.
Optional settings are `MODEL_TEMPERATURE`, `MODEL_MAX_COMPLETION_TOKENS`,
`MODEL_TIMEOUT_SECONDS`, and `MODEL_MAX_RESPONSE_BYTES`. See the
[provider contract](docs/provider.md) for exact supported configuration and
implemented transport/response handling. Live availability and extraction
quality have not been observed. Keep credentials outside source and state.

Prompt: `access_intake/prompt.py`; schema: `snapshot.schema.json`; policy:
`materials/office-policy.md`; source oracle: `tests/expected_facts.json`, frozen
before measuring outputs. Durable records retain producing settings and origin.

## Operating scope

Multiple operators share local SQLite on a single host. Network filesystems and
multi-host coordination are unsupported. Leases prevent stale publication;
uncertain remote calls can repeat after a crash. Mock batches of 400 and 2,000
verify scheduling/storage, not a production latency, quota, availability or cost
SLA. Source-override detection is conservative and not exhaustive.

Capture pairing, push and Classroom submission belong to Step 10; these commands
do not perform submission.
