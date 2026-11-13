# Provider integration

Development uses saved replies in `tests/mock_responses/`. Mock construction
never reads `API_KEY`. T30 exercises the actual HTTP adapter through a fake
transport, including configuration loading with an isolated dummy credential.
No live model call has been performed or claimed.

## Supported request configuration

The adapter implements a single, non-streaming HTTPS POST to a chat-completions
endpoint. The documented OpenAI configuration is:

| Environment variable | Value |
| --- | --- |
| `MODEL_ENDPOINT` | `https://api.openai.com/v1/chat/completions` |
| `MODEL_PROVIDER` | `openai` |
| `MODEL_ID` | `gpt-5.4-mini-2026-03-17` |
| `MODEL_TEMPERATURE` | `0` |
| `MODEL_MAX_COMPLETION_TOKENS` | `2048` |
| `MODEL_TIMEOUT_SECONDS` | `60` |
| `MODEL_MAX_RESPONSE_BYTES` | `1048576` |
| `API_KEY` | Supplied through the environment; never store it in source or configuration files |

This selects the model's default reasoning effort (`none`). The contract follows
the [Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [GPT-5.4 mini model documentation](https://developers.openai.com/api/docs/models/gpt-5.4-mini).
The configuration is implemented and documented, but availability, account access
and live extraction quality have not been tested. Other services must implement
this same request/response contract; compatibility is not assumed.

Call `HttpProvider.from_environment()` explicitly for live construction. It
requires `MODEL_ENDPOINT`, `MODEL_ID` and `API_KEY`. The remaining variables are
optional, with the table's defaults except `MODEL_PROVIDER`, whose default is
`openai-compatible`. Credentials are loaded only from `API_KEY`, kept on the
in-memory adapter, and used in its Authorization header. The serializable
`HttpConfig` contains no credential. There is no implicit live-mode selection.

## Request and response handling

The request contains the configured model, temperature and token limit,
`stream=false`, a system message from `access_intake/prompt.py`, and the exact
decoded source as a separate user message. It requests strict JSON-schema output
with the 14 properties in `snapshot.schema.json`. Export metadata never supplies
business facts. No tool calls are requested.

The transport sends one request, applies the configured socket timeout, caps the
response bytes, and refuses redirects. The adapter also enforces the byte cap on
injected transports. The timeout bounds socket operations; a hard total-attempt
deadline and bounded automatic retry scheduling belong to step 8.

A response must be UTF-8 JSON with exactly one completion choice, an assistant
message, a recognized finish reason and a producing model ID. Refusal takes
precedence even when content contains valid JSON. Only `finish_reason=stop`
counts as completed output. Other recognized reasons are incomplete and cannot
publish successful facts. Local validation then checks extraction JSON, schema,
dates, field-specific null issues and exact source quotes before routing.

HTTP 429/5xx and transport failures carry transient-retry metadata. A timeout has
the distinct `provider-timeout` code. A valid Retry-After number or HTTP date is
parsed; the later scheduler will apply the configured cap and backoff. Other
HTTP errors, oversized responses, malformed envelopes, refusals, incompletion
and invalid extraction data are terminal for that attempt. The adapter itself
does not retry or repair output.

## Failure isolation and retries

`process_documents(paths, provider=...)` isolates provider failures and preserves
an unaffected peer's extraction and review route. `IntakeService` retains source
bytes in memory by attempt ID. Its `retry(attempt_id, provider=...)` uses those
saved bytes through an injectable retry handler, even if the source path changes.
Earlier attempts remain identified by their own attempt IDs.

Error records have a null extraction, IT Service Desk ownership and a retry
description. The `STATE` placeholder in the in-memory service's retry command is
a state location to be supplied. `Repository` provides durable retries as
described below. Executable operator CLI retry commands are step 8.

Provider error bodies and original transport diagnostics are excluded from
operational records. Controlled diagnostics and credential redaction prevent a
transport from echoing the configured key into outputs. The adapter emits no raw
request/response logs. Evidence redacts Authorization and any occurrence of the
configured credential in its payload.

## Durable state (step 6)

`access_intake/repository.py` supplies `Repository(state_path)` and serializable
`ProcessingSettings`. Source bytes, their original-byte hash, the filename,
revision and settings are committed with queued work before provider execution.
Running work is visible to another connection, and a provider call holds no
database write lock. SQLite uses WAL, full synchronous commits and a bounded
five-second lock wait. This step's service executes sequentially; multi-process
claims, leases and crash recovery remain for step 7.

The service methods are:

| Method | Behavior |
| --- | --- |
| `intake(paths, provider=..., settings=...)` | Accept independent documents; reuse identical terminal successes/errors; retain omitted filenames |
| `history(document_id)` | Return every completed attempt, including current, in acceptance order |
| `snapshot()` | Read current extractions and matching queue entries in one transaction |
| `progress()` | Read persisted attempt states, separate from policy status |
| `saved_attempt(attempt_id)` | Inspect the committed source bytes, settings and attempt identity |
| `retry(attempt_id, provider_factory=...)` | Create a new attempt from the saved bytes/settings, even after reopening state or changing the source path |
| `refresh(settings=..., provider_factory=..., router=...)` | Re-extract for changed extraction settings; reroute valid saved facts for a policy-only change |

Attempt completion, its historical result and current-result publication share
one commit. Changed source bytes create a new revision. A failed retry becomes
the current error while retaining the earlier success in history. A retry of a
superseded source stays historical. Export timestamps never select the winning
revision. The current snapshot contains one terminal result per completed
document; earlier versions remain available through history.

Extraction fingerprints cover original bytes, provider/model/endpoint, request
settings, prompt, schema and extraction implementation version. Policy version
is separate. Policy-only refresh uses no provider call and retains the extraction's
producing settings and attempt ID alongside the new routing settings/version.
It leaves extraction errors intact rather than inventing facts. Explicit retry
or a change to extraction settings can re-extract those errors.

For the HTTP adapter, obtain settings with
`ProcessingSettings.from_provider(provider)`. The repository checks that the
injected adapter matches those settings. A retry/refresh factory receives the
saved `ProcessingSettings`; `HttpProvider.from_settings(settings)` reconstructs
the saved model, endpoint, request settings, prompt and schema while loading
`API_KEY` afresh from the environment. Stored settings contain no credential,
and credential fields are rejected. Tests inject the dummy credential through
an in-memory mapping and use only a fake HTTP transport.

Repository errors include the saved attempt ID and the actual, shell-quoted
state path in their retry descriptions. The operator CLI is still step 8.

## Reproducible evidence and verification

From the repository root:

```sh
env -u API_KEY python3 -m tools.record_request_evidence
env -u API_KEY python3 -m unittest discover -s tests -v
```

The first command regenerates `deliverables/request-evidence.json` with the real
adapter, the saved `http-complete.json` reply and a fake transport. Its producing
configuration is `test-provider/test-model`, timeout 12 seconds and response cap
65536 bytes. The saved response reports `test-model-snapshot`; this becomes the
result's model provenance. These are fake identifiers, not a live observation.

T29 compares the failed document and unaffected peer, then retries the saved
bytes through the injected handler. T30 compares exact request settings and
schema, refusal/incompletion, HTTP and transport failures, size limits, producing
model provenance and credential-redacted evidence. Facts come from the frozen
source-derived ledger, `tests/expected_facts.json`. Mock agreement verifies
pipeline behavior, not live extraction accuracy, service availability or quotas.

T31–T35 additionally verify reopen/reuse, committed input before provider calls,
available writer locks during calls, changed and whitespace-only revisions,
omitted documents, saved-source/settings retries, retained successes after
failures, current/history provenance and extraction versus policy refresh.
Their HTTP-binding subcase verifies that saved prompt/schema/request changes
reach the real adapter through a fake transport. Synthetic routing changes in
T35 are labeled test-only; they do not change the supplied office policy.
