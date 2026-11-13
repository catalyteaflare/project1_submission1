# Provider contract (step 5)

Development uses saved envelopes under `tests/mock_responses/`. Tests never
contact a model. T30 loads a dummy credential into an isolated environment and
uses the actual adapter with a fake HTTP transport.

The implemented live contract is a single, non-streaming HTTPS POST to a
chat-completions endpoint. One documented configuration is
`MODEL_ENDPOINT=https://api.openai.com/v1/chat/completions`,
`MODEL_PROVIDER=openai`, `MODEL_ID=gpt-4o-mini-2024-07-18`, temperature 0,
`max_completion_tokens=2048`, and strict JSON-schema output. This configuration
has not been exercised against a live service. The adapter contract follows the
[Chat Completions reference](https://developers.openai.com/api/reference/resources/chat)
and [GPT-4o mini model documentation](https://developers.openai.com/api/docs/models/gpt-4o-mini).
Other endpoints must implement the same contract; compatibility is not assumed.

`HttpProvider.from_environment()` requires `MODEL_ENDPOINT`, `MODEL_ID` and
`API_KEY`. Optional variables are `MODEL_PROVIDER`, `MODEL_TEMPERATURE`,
`MODEL_MAX_COMPLETION_TOKENS`, `MODEL_TIMEOUT_SECONDS` (60), and
`MODEL_MAX_RESPONSE_BYTES` (1048576). Credentials exist only on the in-memory
adapter and Authorization header. They are excluded from processing settings,
evidence, results and controlled error diagnostics. Mock construction reads no
environment credentials. Live construction must be explicitly selected by the
caller; the operator CLI is planned for step 8.

The request has separate system instructions and user message data, the supplied
14-property schema, and no streaming or tools. The response must contain exactly
one choice. Refusal is handled before content, and only `finish_reason=stop`
counts as complete. Local validation then checks output JSON, schema, dates and
exact source quotes. A non-stop reason cannot publish a success, even with valid
JSON. HTTP 429/5xx and transport failures are classified as transient;
Retry-After seconds or an HTTP date are parsed for the later scheduler. Other
HTTP errors, malformed envelopes, refusals and invalid content are terminal.
The adapter performs one call; bounded retry scheduling belongs to step 8.
The transport bounds socket operations with a timeout and caps response bytes;
the scheduler will enforce the total attempt deadline. Redirects are refused.

`deliverables/request-evidence.json` is generated from the adapter's request
builder for T30's `test-provider/test-model` configuration and fake transport.
It contains a redacted Authorization header. It is not a live observation.

Source override inspection is conservative: quoted override examples can be
sent to human inspection. Pattern matching is not exhaustive injection detection.
The independent fact ledger is `tests/expected_facts.json`; mock agreement does
not measure live extraction accuracy.

Step 6 adds `Repository` and `ProcessingSettings` in
`access_intake/repository.py`. SQLite stores source bytes and settings before
provider execution, then commits the completed attempt and current pointer
together. `history(document_id)` returns completed attempts in acceptance order,
including the current one; `snapshot()` returns only current extractions and
their matching queue entries. Omitting a filename from later intake preserves it.

For a real adapter, construct settings with
`ProcessingSettings.from_provider(provider)` so the saved model, endpoint,
request limits, prompt and schema match the actual request. Mismatched adapters
are rejected. A retry/refresh provider factory receives the saved settings;
`HttpProvider.from_settings(settings, transport=...)` rebuilds that exact
configuration while loading the credential afresh from the environment.
Tests inject fake transports and dummy credentials only.

Extraction fingerprints include original bytes, provider/model/endpoint, request
settings, prompt, schema and extraction version. Policy versions are separate.
Unchanged terminal successes/errors are reused. Policy-only refresh reroutes a
saved valid extraction without a provider call and preserves its producing
settings; errors require explicit retry or an extraction-setting change. A retry
of a superseded source remains historical. Multi-process claims, leases and crash
recovery tests are step 7; the operator CLI and scheduler are step 8.

Run the current offline suite from the repository root:

```sh
env -u API_KEY python3 -m unittest discover -s tests
```
