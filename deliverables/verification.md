# Verification record

## Scope, configuration and origin

This report distinguishes intended assertions from actual observations. Runtime:
Observed host: CPython 3.12.3, Linux, SQLite 3.45.1; supported runtime is
Python 3.11+ POSIX, standard library, local SQLite WAL/full synchronous state.
Offline walkthrough configuration: provider mock, model saved-envelope, prompt
extraction-v1, policy northstar-v1, shipped schema and config/mock-map.json;
four workers, 60-second total attempt deadline, at most three calls per attempt.
Responses are saved fixtures in tests/mock_responses/, never a live model.
Adapter checks use the real HTTP adapter with a fake transport, dummy in-memory
credentials and saved HTTP replies; request-evidence.json names its exact test
configuration. Each exported record and accuracy observation records producing
settings and response_source. No real API_KEY is needed or read by mock tests.

## Commands and intended comparisons

Execute the complete offline walkthrough in README.md for locked installation,
intake, paginated queue/progress/history inspection, pinned export/copy and the
21-case accuracy run. T41 executes those exact commands in a clean temporary
source copy, excluding local state/environments, with inherited network and key
guards. It checks the empty requirements.lock using a new venv and pip with
--no-index --require-hashes, so dependency acquisition cannot hide missing setup.

```sh
env -u API_KEY python3 -m unittest discover -s tests
env -u API_KEY python3 -m unittest discover -s tests -p test_handover.py -v
env -u API_KEY python3 -m tools.record_request_evidence
env -u API_KEY python3 -m access_intake accuracy --state .runtime/accuracy.sqlite --expected tests/expected_facts.json
```

Intended assertions: nonzero test count and no unexpected failure; final exports
have exactly 20 unique supplied filenames and their original-byte hashes;
extraction/queue identity, revision and attempt agree; copies equal one verified
generation byte for byte; 21 source-derived cases give denominator 252; missing
records and failed extractions stay in that denominator. Deliberately altered
measurements in T41 must give 224/252, 18/21 routing and 28 scalar comparison
misses, while evidence and issues are reported separately. The evidence command
must reproduce the committed redacted actual-adapter request artifact exactly.

## Actual observations (2026-11-13 UTC)

- Previous step baseline: 43 tests passed; the test-first T41 run had 44 tests
  with one expected failure because five handover artifacts were absent.
- The Step 9 implementation generated the 20-record final pair with the mock
  configuration above. Its generation/checksums are in snapshot-manifest.json.
- The separate 21-case saved-mock accuracy run observed 252/252 scalar matches,
  21/21 routing, no comparison misses, no evidence failures and no issue failures.
- Targeted T41 passed in 3.116 seconds: fresh source copy and environment,
  exact README walkthrough, 20-record verified generation copy, 21-case CLI
  accuracy, reproduced fake-transport evidence, and adversarial scorer checks.
- Final full-suite command above: **44 tests passed in 34.279 seconds**, exit 0,
  with no skipped cases on this host. Output is preserved in
  `planning/test-run.txt`. This includes the 400- and 2,000-message mock
  scheduling checks, not a live workload.
- The final handover files' SHA-256 values were independently recomputed and
  matched `deliverables/snapshot-manifest.json`. `git diff --check` passed.
  The mapping below states intended assertions; their execution was observed
  in this full-suite run with the offline configurations described above.
- Setup repair: the first clean walkthrough found that this host lacks
  ensurepip. README now creates venv with --without-pip and uses installed host
  pip's --python option for the offline locked installation. No dependencies
  were downloaded and no application expectations changed.

No live model quality, availability or latency was measured. No repaired model
response was accepted or used to revise the frozen source oracle. No optional
live run is mixed with mock observations.

## Verification-to-test mapping (intended assertions)

Each ID belongs to its original test plan; descriptive comparisons below come
from planning/plan.md. T02–T21/T27 expectations were authored from message/policy
quotations before measured output. Subsequent process/transport cases retain
those same business facts. Starter tests additionally check supplied files/schema
and a saved-reply load.

| Case | Situation | Intended comparison/assertion |
| --- | --- | --- |
| T01 Mock provider | IT-001.json through the provider interface only | Returns the saved completed envelope and mock provenance; call count increments once; errors/delays are controllable with no key/network. No routing asserted yet. |
| T02 Complete request | IT-001.json | IT-001; 2026-10-22; new; Maya Chen; Analytics; Atlas; staging; read; 2026-10-23; 2026-11-06; Priya Shah. Purpose: “checking the weekly conversion dashboard with the new event definition changes”. OWNER. |
| T03 Production/write | IT-002.json | IT-002; 2026-10-22; new; Owen Reed; Payments; Ledger; production; write; 2026-10-24; 2026-10-25; Elena Park. Purpose: “make the corrections at the time of our scheduled maintenance and then check the totals against the settlement report”. RISK. |
| T04 Missing dates and access details | IT-003.json | IT-003; 2026-10-22; new; Lina Gomez; Customer Success; Beacon; null; null; null; null; Priya Shah. Purpose: “get the account notes together before we meet as a team”. Separate issues for environment, permission and both dates; CLARIFY. |
| T05 Conflicting details | IT-004.json | IT-004; 2026-10-22; new; Theo Malik; Quality Engineering; Atlas; null; read; 2026-10-23; 2026-10-30; Nia Patel. Purpose: “understand why the nightly quality report shows different counts from the release checklist”. Environment conflict issue and both alternatives in evidence; CLARIFY. |
| T06 Withdrawal | IT-005.json | IT-005; 2026-10-22; withdrawal; Ava Brooks; Design; null; null; null; null; null; Elena Park. Purpose: “The project that triggered my request has been cancelled so now there is no work for me to do.” Missing system/environment/permission/start/end issues; WITHDRAW rather than clarification. |
| T07 Embedded instructions | IT-006.json | IT-006; 2026-10-22; new; Finn Lee; Platform; Atlas; staging; read; 2026-10-23; 2026-10-24; Nia Patel. Purpose: “look at deployment metrics and compare them against release notes before service review”. Override issue with exact evidence; INSPECT; never approved, admin or credential disclosure. |
| T08 Change to write | IT-007.json | IT-007; 2026-10-22; change; Grace Tan; Developer Experience; Forge; staging; write; 2026-10-24; 2026-10-28; Nia Patel. Purpose: “updating starter project templates”. RISK. |
| T09 Corrected environment | IT-008.json | IT-008; 2026-10-22; new; Noah Kim; Finance; Atlas; staging; read; 2026-10-25; 2026-10-31; Priya Shah. Purpose: “check the sample billing total vs forecasting workbook before month end rehearsal”. Correction evidence, no unresolved environment conflict; OWNER. |
| T10 Missing manager | IT-009.json | IT-009; 2026-10-22; new; Isla Morgan; Support; Beacon; staging; read; 2026-10-26; 2026-10-29; null. Purpose: “go through example support cases before updating our troubleshooting guide”. Manager missing issue; CLARIFY. |
| T11 Missing end date | IT-010.json | IT-010; 2026-10-22; new; Ben Carter; Operations; Ledger; production; read; 2026-10-27; null; Elena Park. Purpose: “compare the daily reconciliation report to the entries that the operations folks see”. End-date issue; CLARIFY takes precedence over RISK. |
| T12 Relative dates | IT-011.json | IT-011; 2026-10-22; new; Zoe Adams; Research; Atlas; staging; read; null; null; Priya Shah. Purpose: “looking at sample experiment records to get ready for our team walkthrough”. Both date issues; CLARIFY without anchoring to exported_at. |
| T13 Staging admin | IT-012.json | IT-012; 2026-10-22; new; Ravi Shah; Infrastructure; Forge; staging; admin; 2026-10-26; 2026-10-27; Nia Patel. Purpose: “doing a configuration migration rehearsal before planning the wider rollout”. RISK. |
| T14 Production read | IT-013.json | IT-013; 2026-10-22; new; Emma Wilson; Compliance; Beacon; production; read; 2026-10-28; 2026-10-30; Elena Park. Purpose: “review account activity records to help with internal audit”. RISK. |
| T15 Reversed dates | IT-014.json | IT-014; 2026-10-22; new; Lucas Brown; Sales Operations; Atlas; staging; read; 2026-11-05; 2026-11-02; Priya Shah. Purpose: “giving a demo of the new territory report for sales leads”. Retain both dates; ordering issue; CLARIFY. |
| T16 Corrected dates | IT-015.json | IT-015; 2026-10-22; new; Mia Davis; Learning; Beacon; staging; read; 2026-10-29; 2026-10-31; Elena Park. Purpose: “make sure that the sample conversations in the examples I'm building match up to the exercise handout for the Support onboarding workshop”. OWNER. |
| T17 Identifier mismatch | IT-016.json | IT-061; 2026-10-22; new; Alex Turner; Platform; Forge; staging; read; 2026-10-26; 2026-10-28; Nia Patel. document_id stays IT-016.json; mismatch issue; purpose: “looking at the example pipeline runs to figure out why the starter project takes longer to build some of the time”. CLARIFY. |
| T18 Other message | IT-017.json | IT-017; 2026-10-22; other; Chloe Martin; Communications; null; null; null; null; null; null. Purpose: “update the document before the next bunch start so they're all doing the same thing”. Issues identify absent access facts/manager; CLARIFY. |
| T19 Longer complete request | IT-018.json | IT-018; 2026-10-22; new; Daniel Wright; Data Engineering; Atlas; staging; read; 2026-10-27; 2026-11-03; Priya Shah. Purpose: “reviewing our sample dataset we use during our weekly analyst workshop”. OWNER, retaining requested read despite mention of updating queries. |
| T20 Change to read | IT-019.json | IT-019; 2026-10-22; change; Sara Ahmed; Customer Operations; Beacon; production; read; 2026-10-28; 2026-11-04; Elena Park. Purpose: “reviewing the daily exception report”. RISK due to production, not old write permission. |
| T21 Conflicting managers | IT-020.json | IT-020; 2026-10-22; new; Ethan Clark; Product; Atlas; staging; read; 2026-10-29; 2026-11-02; null. Purpose: “look at some sample usage records before I draft the summary on feature adoption”. Manager issue and both names in evidence; CLARIFY. |
| T22 Input contract and independent documents | IT-001.json and IT-005.json; temporary new export IT-901.json with message “IT-901, 2026-10-22: Café team needs help.”; IT-001 metadata/whitespace variants; IT-016.json; temporary second filename containing the unchanged IT-001 message; malformed inputs and duplicate basename batch | Provider receives exactly the decoded message, including newlines and é; metadata changes do not change the decoded extraction source; business-fact invariance is checked later against T02. Hash reflects raw bytes, including whitespace; identity remains filename. Two filenames containing the same message create separate input jobs; mismatch extraction/issues are asserted later in T17, not in this loader test. Invalid UTF-8/JSON or non-string/missing message yields an actionable input error without blocking valid peers. Reject duplicate basenames before processing. |
| T23 Schema and semantic validation | IT-001.json; mutate only provider output in separate subcases | Complete object accepted; schema-permitted nulls accepted structurally. Bad shapes, impossible/non-ISO dates, empty scalars or a quote absent from decoded source give ERROR before routing. Arrays contain only strings; null scalar uncertainty requires issues. Preserve real reversed dates for T15 clarification. |
| T24 Refusal | IT-001.json | ERROR, actionable refusal diagnostic, null extraction; no successful route or partially accepted fields. |
| T25 Interrupted response | IT-001.json | ERROR for both; null extraction even when content parses; completion state checked before use. |
| T26 Malformed output | IT-001.json | ERROR with parse/envelope diagnostic and null extraction; explicit refusal/incomplete/malformed causes stay distinguishable. |
| T27 Paraphrased synthetic request | Temporary new IT-901.json with authored message below, paraphrasing the real IT-001.json | IT-901; 2026-10-22; new; Maya Chen; Analytics; Atlas; staging; read; 2026-10-23; 2026-11-06; Priya Shah. Purpose: “compare the weekly conversion dashboard with the revised event definitions”. Exact evidence from new wording, no sample-file lookup; OWNER. |
| T28 Routing precedence | IT-006, IT-005, IT-010, IT-001, IT-002 exports plus authored overlap messages below and a null-kind variant of IT-017 | Ordered outcomes: invalid extraction ERROR; override+withdrawal INSPECT; withdrawal with ID/date but no other facts WITHDRAW; missing end date in production CLARIFY; complete production/write RISK; complete staging/read OWNER; null/other kind CLARIFY. Never approval. |
| T29 Provider failure and unaffected peer | IT-001.json and IT-002.json together | IT-001 ERROR with an identified document and a reproducible retry-command description; IT-002 keeps T03 fields/RISK. The injected service-level retry handler succeeds with T02 facts/OWNER; execution of the documented CLI retry command is asserted later in T40. No global batch rollback. |
| T30 Real adapter contract, configuration and redaction | IT-001.json through the real adapter with fake HTTP transport; in-memory dummy environment credential | Configured endpoint/model/settings and separated instructions/message reach the transport; schema request matches adapter contract; completion/refusal/errors map correctly. T02 facts/OWNER on success, ERROR on terminal failures. API_KEY read from environment only; credential absent from logs, state, outputs and evidence. `response_source` identifies actual adapter/provider/model when used, while test observations explicitly say fake transport. |
| T31 Unchanged rerun | IT-001.json twice, identical relevant settings | T02 fields/OWNER retained; one current extraction and queue entry; no second provider call or replacement result. Terminal errors reused until explicit retry, as OQ-04. |
| T32 Changed source content | IT-001.json followed under the same filename by original message plus “Correction: replace the end date with 2026-11-07.”; separate whitespace-only revision subcase | Current T02 facts except end_date=2026-11-07, OWNER; new hash/result. Earlier T02 result/hash retained in separate history. Whitespace alone also changes hash/result but preserves business facts. Queue and extraction refer to same revision. |
| T33 Incremental omission | Batch 1 IT-001.json and IT-002.json; batch 2 IT-002.json only | Both current records remain with T02/OWNER and T03/RISK. IT-001 omission causes no withdrawal or deletion. |
| T34 Failed retry after prior success | IT-001.json succeeds; explicit saved-attempt retry fails; separate subcase retry of T32 revised source fails | Latest current entry ERROR with null extraction, actionable diagnostic and latest attempt/hash. Prior success stays in history with earlier attempt ID/settings/hash and its T02 or T32 facts. Same-source retry distinguishes attempts even when hashes match. |
| T35 Configuration and policy refresh | IT-001.json; change model/prompt/schema/request settings separately, then policy version | Changed extraction fingerprint makes a new result; unchanged fingerprints reuse. Policy-only refresh reroutes saved extraction without a model call. Current/history retain exact producing settings and policy versions. Under unchanged supplied rules facts/route remain T02/OWNER; any synthetic policy behavior is labeled a test-only configuration, never office policy. |
| T36 Overlapping operators | Two worker-service processes (test harness, before the operator CLI exists) share state: overlapping IT-001 and distinct IT-002; repeat crash/lease expiry with barriers | One current pointer per filename; all committed attempts retained; independent peers complete; no stale claimant can publish after replacement. Duplicate remote calls allowed only as documented after uncertain recovery; never duplicate current publication. Facts remain T02/OWNER and T03/RISK. |
| T37 Interruption and snapshot consistency | IT-001/IT-002 intake; kill at recorded job claim, response-before-commit, result commit, generation write and manifest publication | Persisted completions survive; unfinished jobs remain identifiable/reclaimable; resume does not reprocess committed successes. Queue/extraction share attempt/hash. A snapshot reader sees a complete old or complete new generation, never a mixed pair; abandoned generations ignored. Business values T02/T03. |
| T38 Reverse completion order | Accept IT-001 original, then T32 corrected revision; complete attempts in reverse order; repeat with newer failure | Newer accepted revision wins regardless of completion time/export timestamp. Late old success stays historical; it cannot overwrite newer end_date=2026-11-07 or newer ERROR. Revision and attempt provenance visible. |
| T39 Operator start/inspect/resume and slow peer | CLI intake IT-001/IT-002; hold IT-001 pending while IT-002 finishes; interrupt/restart | While one call is pending, queue displays IT-002 T03/RISK status/owner/action; progress identifies IT-001's unfinished state and hash. Resume later produces T02/OWNER. Prior results in a refresh are visibly stale, never attributed to new source. Display all six route/error outcomes from earlier fixtures. |
| T40 Outage, retry limits and workload | IT-001/IT-002; scripted slow/429/503/unavailable service; temporary copies with corrected IDs for 400 and 2,000 documents | Pending/retry-wait/error attempts locatable; retry command reuses stored bytes/settings. At most configured workers and call attempts; waits do not monopolize work slots; completed peers remain inspectable. Exact ID correction in each copy prevents accidental mismatch. No lost/duplicate current records after resume; facts/routes inherited from T02/T03. Report capacity checks without claiming a latency SLA. |
| T41 Package, deliverables, accuracy and handover | All 20 actual exports, T27 paraphrase, clean temporary installation/state and documented mock walkthrough | Runnable locked setup, prompt/schema/config and exact README commands exist. Current extractions: all 20 unique filenames, correct raw hashes, required wrappers, mock provenance; queue joins same attempt/source. Source ledger covers all 20 plus paraphrase. Accuracy command independently counts exact matches/errors as below. Evidence is redacted and agrees with adapter. Verification labels commands, assertions, actual observations/config/origin and operating limits. |

## Operating limits and interruption behavior

Single-host shared local SQLite is the supported coordination boundary. Network
filesystems, separate regional databases and multi-host safety are not verified.
Durable acceptance order determines revisions; export timestamps do not reconcile
upstream chronology. Pending source work is distinct from old completed results.
Finite leases, claim tokens and current-pointer checks fence late publication.
An interruption between remote execution and commit can cause duplicate remote
calls; exactly-once remote execution is not promised. Resume recovers saved
inputs/settings while completed peers remain inspectable. SIGTERM cleanup and
hard deadlines are checked with controlled worker processes; fault points check
before/after commit and snapshot publication, not physical power loss.

T40 instruments actual calls and checks bounded concurrency with 400 and 2,000
mock documents, blocked peers, Retry-After, transient outage, persisted backoff,
call limits and timeouts. This measures scheduler/storage correctness only;
there is no verified production throughput, latency SLA, provider quota or cost
target. Timeouts and worker count must be chosen for deployment constraints.
POSIX process tests are skipped on unsupported platforms. Source-override checks
are conservative and not exhaustive injection detection; quoted examples can
produce false positives. Exact evidence membership cannot establish every fact.

Fixed handover filenames are replaced separately after both generation files
are verified. Concurrent readers must use the manifest-based snapshot interface;
it resolves a single generation and verifies both hashes. Interrupted handover
copies can be regenerated; they are not the application coordination interface.

## C01 / Step 10 submission record

Submitted application commit: `91e20ce0ded0c3b0933155bca5fd1e70d13239ff`.
GitHub's default branch `main` and remote HEAD were verified at that exact
commit with `git remote show origin` and `git ls-remote origin`.
Required application/output files were inspected in the commit and matched
the clean working tree. The 20-record handover pair matched its manifest hashes.

The commit's `Entire-Checkpoint` trailer identifies
`01M7AW63AVP5WZJ3TDEABJQ489`. Entire metadata paired that checkpoint with the
coding session. Its ref, `refs/entire/checkpoints/89/01M7AW63AVP5WZJ3TDEABJQ489`,
was verified on the remote with the same object as the local capture; the
capture contains session metadata and transcripts. No capture setup was changed.

The final pre-submission check ran all 44 tests successfully in 36.581 seconds,
using the same offline mock/fake-transport configuration described above.
The repository owner subsequently confirmed completing the Classroom
submission. The instructed submission text was only the root repository URL:
`https://github.com/catalyteaflare/ai-full-stack-it-access-requests`.
Classroom completion is owner-confirmed; its receipt and exact submitted text
were not independently inspected through a Classroom connection.

This documentation update records the submission of the commit above; it does
not change the identity of the verified submitted application commit.
