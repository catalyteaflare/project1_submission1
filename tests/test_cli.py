"""T39/T40: actual CLI entry point, bounded scheduling and offline workloads.

Facts/routes come from the frozen ledger. Tests use saved replies and authored
ID substitutions, with network/key guards inherited by forked workers and
installed explicitly in fresh CLI interpreters. Events and logical clocks
control ordering; workload sizes are capacity checks, not latency targets.
"""

import hashlib
import io
import json
import multiprocessing
import os
import shlex
import socket
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from access_intake.providers import MockOutcome, MockProvider, ProviderUnavailable


ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "materials" / "requests"
MOCKS = ROOT / "tests" / "mock_responses"
LEDGER = json.loads((ROOT / "tests/expected_facts.json").read_text(encoding="utf-8"))
CTX = multiprocessing.get_context("fork") if "fork" in multiprocessing.get_all_start_methods() else None


def saved(name):
    return json.loads((MOCKS / f"{name}.json").read_text(encoding="utf-8"))


class HeldProvider:
    response_source = "mock"

    def __init__(self, entered, release):
        self.entered, self.release = entered, release

    def extract(self, source):
        if source == LEDGER["T02"]["message"]:
            outcome = MockOutcome(response_path=MOCKS / "complete.json", entered=self.entered, wait_for=self.release)
        elif source == LEDGER["T03"]["message"]:
            outcome = MockOutcome(response_path=MOCKS / "production-write.json")
        else:
            raise AssertionError("No saved reply configured for the held-provider input")
        return MockProvider([outcome]).extract(source)


class CountedProvider:
    """Observe actual calls/concurrency instead of trusting scheduler metrics."""

    def __init__(self, base, total, *, active=None, peak=None, ready=None, release=None):
        self.base, self.total = base, total
        self.active, self.peak, self.ready, self.release = active, peak, ready, release

    def __getattr__(self, name):
        return getattr(self.base, name)

    def extract(self, source):
        with self.total.get_lock():
            self.total.value += 1
            number = self.total.value
        if self.active is not None:
            with self.active.get_lock():
                self.active.value += 1
                with self.peak.get_lock():
                    self.peak.value = max(self.peak.value, self.active.value)
                if self.active.value == 4:
                    self.ready.set()
        try:
            if self.release is not None and number <= 4 and not self.release.wait(5):
                raise AssertionError("First workload wave was not released")
            return self.base.extract(source)
        finally:
            if self.active is not None:
                with self.active.get_lock():
                    self.active.value -= 1


def intake_worker(arguments, entered, release, peer_completed):
    from access_intake.cli import main

    def hook(stage, job):
        if stage == "completed" and job["document_id"] == "IT-002.json":
            peer_completed.set()

    with redirect_stdout(io.StringIO()):
        code = main(arguments, provider_factory=lambda _: HeldProvider(entered, release), hook=hook)
    raise SystemExit(code)


@unittest.skipUnless(CTX is not None, "CLI process-barrier tests require POSIX fork")
class OperatorContract(unittest.TestCase):
    def setUp(self):
        original_getitem = type(os.environ).__getitem__

        def guarded_getitem(environ, key):
            if key == "API_KEY":
                raise AssertionError("CLI/scheduler tests must not read API_KEY from os.environ")
            return original_getitem(environ, key)

        guard = patch.object(type(os.environ), "__getitem__", guarded_getitem)
        guard.start()
        self.addCleanup(guard.stop)
        for name in ("socket", "create_connection", "getaddrinfo"):
            guard = patch.object(socket, name, side_effect=AssertionError("Network denied in CLI/scheduler tests"))
            guard.start()
            self.addCleanup(guard.stop)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "state.sqlite"

    def manifest(self, sources):
        path = self.directory / "mock-map.json"
        path.write_text(json.dumps({
            hashlib.sha256(source.encode("utf-8")).hexdigest(): {"response": str(MOCKS / f"{fixture}.json")}
            for source, fixture in sources.items()
        }), encoding="utf-8")
        return path

    def invoke(self, arguments, **injections):
        from access_intake.cli import main
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([str(value) for value in arguments], **injections)
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def fresh_cli(self, arguments):
        # Exclude the key before reading any caller environment values.
        environment = {name: os.environ[name] for name in os.environ if name != "API_KEY"}
        environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "tests/offline_guard"), str(ROOT)))
        # Assert guard installation before executing the actual package entry point.
        launch = ("import os,runpy; assert os.environ.get('ACCESS_INTAKE_OFFLINE_TEST') == '1'; "
                  "runpy.run_module('access_intake', run_name='__main__', alter_sys=True)")
        completed = subprocess.run([sys.executable, "-c", launch, *map(str, arguments)],
                                   cwd=ROOT, env=environment, text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def assert_business(self, record, case="T02", *, request_id=None, end=None, origin="mock"):
        expected = LEDGER[case]
        self.assertIsNone(record["error"], record)
        self.assertEqual(record["response_source"], origin)
        self.assertEqual([record[name] for name in ("status", "owner", "next_action")], expected["route"])
        for field, value in expected["scalars"].items():
            if field == "request_id" and request_id is not None:
                value = request_id
            if field == "end_date" and end is not None:
                value = end
            self.assertEqual(record["extraction"][field], value, (record["document_id"], field))

    def assert_error(self, record, code):
        self.assertIsNone(record["extraction"])
        self.assertEqual((record["status"], record["owner"], record["next_action"]),
                         ("error", "IT Service Desk", "retry-extraction"))
        self.assertEqual(record["error"]["code"], code)

    def test_T39(self):
        from access_intake.cli import main
        from access_intake.repository import Repository
        from access_intake.snapshots import read_snapshot

        manifest = self.manifest({LEDGER["T02"]["message"]: "complete", LEDGER["T03"]["message"]: "production-write"})
        entered, release, peer_completed = CTX.Event(), CTX.Event(), CTX.Event()
        arguments = ["intake", str(REQUESTS / "IT-001.json"), str(REQUESTS / "IT-002.json"),
                     "--state", str(self.state), "--mock-map", str(manifest), "--workers", "2"]
        child = CTX.Process(target=intake_worker, args=(arguments, entered, release, peer_completed))
        child.start()

        def cleanup():
            # A killed event waiter can abandon its lock; do not set release here.
            if child.is_alive():
                child.terminate()
            child.join(5)
            if child.is_alive():
                child.kill()
                child.join(5)
        self.addCleanup(cleanup)
        self.assertTrue(entered.wait(5))
        self.assertTrue(peer_completed.wait(5))
        queue = self.fresh_cli(["queue", "--state", self.state])
        self.assertEqual(queue["total"], 1)
        self.assertEqual(queue["items"][0]["document_id"], "IT-002.json")
        self.assertEqual([queue["items"][0][key] for key in ("status", "owner", "next_action")], LEDGER["T03"]["route"])
        progress = self.fresh_cli(["progress", "--state", self.state])
        pending = next(row for row in progress["items"] if row["document_id"] == "IT-001.json")
        self.assertEqual(pending["work_state"], "running")
        self.assertEqual(pending["source_sha256"], hashlib.sha256((REQUESTS / "IT-001.json").read_bytes()).hexdigest())
        self.assertTrue(pending["attempt_id"])
        self.assertIn("revision", pending)
        self.assertIn("settings", pending)

        child.terminate()
        child.join(5)
        self.assertFalse(child.is_alive(), "CLI must shut down its owned workers on interruption")
        self.assertIn(child.exitcode, (0, 130, 143))
        interrupted = self.fresh_cli(["progress", "--state", self.state])
        pending = next(row for row in interrupted["items"] if row["document_id"] == "IT-001.json")
        self.assertEqual(pending["work_state"], "interrupted")
        self.assertEqual(interrupted["counts"].get("interrupted"), 1)
        self.assertEqual(interrupted["counts"].get("running", 0), 0)
        with Repository(self.state) as repo:
            stored = next(row for row in repo.progress() if row["document_id"] == "IT-001.json")
            self.assertEqual(stored["work_state"], "running", "CLI labels must not alter the stored state before recovery")
        self.fresh_cli(["resume", "--state", self.state])
        queue = self.fresh_cli(["queue", "--state", self.state, "--limit", "1", "--offset", "0"])
        self.assertEqual(queue["total"], 2)
        self.assertEqual(len(queue["items"]), 1)
        self.assertEqual(queue["items"][0]["document_id"], "IT-001.json")
        self.assertFalse(queue["items"][0]["stale"])

        revised = self.directory / "IT-001.json"
        revised_source = LEDGER["T02"]["message"] + " Correction: replace the end date with 2026-11-07."
        revised.write_text(json.dumps({"message": revised_source}), encoding="utf-8")
        manifest = self.manifest({revised_source: "changed-end"})
        self.fresh_cli(["intake", revised, "--state", self.state, "--mock-map", manifest, "--enqueue-only"])
        stale = self.fresh_cli(["queue", "--state", self.state])["items"][0]
        self.assertTrue(stale["stale"])
        self.assertNotEqual(stale["source_sha256"], hashlib.sha256(revised.read_bytes()).hexdigest())
        pending = self.fresh_cli(["progress", "--state", self.state])["items"][0]
        self.assertEqual(pending["work_state"], "queued")
        self.assertGreater(pending["revision"], stale["revision"])
        self.fresh_cli(["resume", "--state", self.state])
        self.assertFalse(self.fresh_cli(["queue", "--state", self.state])["items"][0]["stale"])
        history = self.fresh_cli(["history", "--document", "IT-001.json", "--state", self.state, "--limit", "1"])
        self.assertGreaterEqual(history["total"], 2)
        self.assertEqual(len(history["items"]), 1)
        with Repository(self.state) as repo:
            self.assert_business(repo.snapshot()["extractions"][0], end="2026-11-07")

        # Render each policy/error outcome via the operator CLI.
        manifest = self.manifest({LEDGER[case]["message"]: LEDGER[case]["fixture"] for case in ("T04", "T06", "T07")})
        self.fresh_cli(["intake", REQUESTS / "IT-003.json", REQUESTS / "IT-005.json", REQUESTS / "IT-006.json",
                        "--state", self.state, "--mock-map", manifest])
        manifest = self.manifest({LEDGER["T05"]["message"]: "conflicting-environment"})
        self.invoke(["intake", REQUESTS / "IT-004.json", "--state", self.state, "--mock-map", manifest],
                    provider_factory=lambda _: MockProvider([MockOutcome(error=ProviderUnavailable("scripted outage"))]),
                    scheduler_options={"base_delay": 0})
        queue = self.fresh_cli(["queue", "--state", self.state])
        self.assertEqual({row["next_action"] for row in queue["items"]}, {
            "review-request", "review-risk-then-owner", "confirm-withdrawal", "clarify", "inspect-message", "retry-extraction"})
        self.assertEqual(queue["total"], 6)

        self.fresh_cli(["refresh", "--state", self.state, "--policy-version", "test-version-same-office-rules", "--enqueue-only"])
        forbidden = Mock(side_effect=AssertionError("Policy-only work constructed a provider"))
        self.invoke(["resume", "--state", self.state], provider_factory=forbidden)
        forbidden.assert_not_called()
        self.fresh_cli(["export", "--state", self.state, "--out", self.directory / "exports"])
        exported = read_snapshot(self.directory / "exports")
        self.assertEqual(len(exported["extractions"]), 6)
        current = {row["document_id"]: row for row in exported["extractions"]}
        for queued in exported["queue"]:
            for key in ("attempt_id", "source_sha256", "revision"):
                self.assertEqual(queued[key], current[queued["document_id"]][key])
        documentation = (ROOT / "docs/operator.md").read_text(encoding="utf-8")
        for token in ("intake", "queue", "progress", "history", "retry", "resume", "refresh", "export",
                      "--state", "--mock-map", "--enqueue-only", "API_KEY"):
            self.assertIn(token, documentation)

    def test_T40(self):
        from access_intake.scheduler import Scheduler, retry_delay
        from access_intake.http_provider import HttpProvider, HttpReply, ProviderFailure
        from access_intake.repository import Repository, ProcessingSettings

        self.assertEqual(retry_delay(ProviderFailure("http-429", "limited", retryable=True, retry_after=1000), 1, jitter=lambda: 0), 60)
        self.assertEqual(retry_delay(ProviderFailure("http-503", "unavailable", retryable=True), 2, jitter=lambda: 0), 1)
        self.assertLessEqual(retry_delay(ProviderFailure("http-503", "unavailable", retryable=True), 8, jitter=lambda: 1), 10)
        clock = [100.0]
        counters = CTX.Array("i", [0, 0])

        def http_factory(settings):
            def fake_transport(**request):
                source = json.loads(request["body"])["messages"][1]["content"]
                index = 0 if source == LEDGER["T02"]["message"] else 1
                with counters.get_lock():
                    counters[index] += 1
                    number = counters[index]
                fixture = ("http-production-write" if index else
                           "http-429" if number == 1 else "http-503" if number == 2 else "http-complete")
                reply = saved(fixture)
                return HttpReply(reply["status"], reply["headers"], json.dumps(reply["body"]).encode())
            return HttpProvider.from_settings(settings, environment={"API_KEY": "dummy-in-memory-T40-only"}, transport=fake_transport)

        adapter = HttpProvider.from_environment(environment={
            "API_KEY": "dummy-in-memory-T40-only", "MODEL_ENDPOINT": "https://example.invalid/v1/chat/completions",
            "MODEL_ID": "test-model", "MODEL_PROVIDER": "test-provider",
        }, transport=lambda **_: None)
        settings = ProcessingSettings.from_provider(adapter)
        with Repository(self.state, clock=lambda: clock[0]) as repo:
            repo.enqueue([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"], settings=settings)
            delays, completed = [], []
            def advance(seconds):
                self.assertIn("IT-002.json", completed, "Backoff must leave capacity available to the peer")
                delays.append(seconds)
                clock[0] += seconds
            def observe(stage, job):
                if stage == "completed":
                    completed.append(job["document_id"])
                    if job["document_id"] == "IT-002.json":
                        first = next(row for row in repo.progress() if row["document_id"] == "IT-001.json")
                        self.assertEqual(first["work_state"], "retry-wait")
            metrics = Scheduler(repo, provider_factory=http_factory, workers=1, clock=lambda: clock[0],
                                wait=advance, jitter=lambda: 0, hook=observe).run()
            self.assertEqual(list(counters), [3, 1])
            self.assertEqual(delays, [2, 1])
            self.assertEqual(metrics["max_active"], 1)
            self.assertEqual(metrics["calls"], 4)
            current = repo.snapshot()["extractions"]
            self.assert_business(current[0], origin="test-provider/test-model-snapshot")
            self.assert_business(current[1], "T03", origin="test-provider/test-model-snapshot")
            self.assertEqual([row["call_count"] for row in repo.progress()], [3, 1])
            with self.assertRaises(ValueError):
                Scheduler(repo, provider_factory=http_factory, max_calls=4)
            with self.assertRaises(ValueError):
                Scheduler(repo, provider_factory=http_factory, timeout=float("nan"))

        # Retry-wait, remaining call budget and deadline survive a restart.
        self.state = self.directory / "backoff.sqlite"
        clock[0] = 100
        counters[0] = 0
        class StopAfterBackoff(Exception):
            pass
        def stop_after_backoff(stage, job):
            if stage == "retry-wait":
                raise StopAfterBackoff
        with Repository(self.state, clock=lambda: clock[0]) as repo:
            repo.enqueue([REQUESTS / "IT-001.json"], settings=settings)
            with self.assertRaises(StopAfterBackoff):
                Scheduler(repo, provider_factory=http_factory, workers=1, clock=lambda: clock[0],
                          jitter=lambda: 0, hook=stop_after_backoff).run()
            pending = repo.progress()[0]
            self.assertEqual(pending["work_state"], "retry-wait")
            self.assertEqual(pending["call_count"], 1)
            self.assertEqual(pending["next_run_at"], 102)
            self.assertEqual(pending["deadline"], 160)
        clock[0] = 101
        waits = []
        def advance_resumed(seconds):
            waits.append(seconds)
            clock[0] += seconds
        with Repository(self.state, clock=lambda: clock[0]) as repo:
            Scheduler(repo, provider_factory=http_factory, workers=1, clock=lambda: clock[0],
                      wait=advance_resumed, jitter=lambda: 0).run()
            self.assertEqual(waits, [1, 1])
            self.assertEqual(counters[0], 3)
            self.assertEqual(repo.progress()[0]["call_count"], 3)
            self.assertEqual(repo.progress()[0]["deadline"], 160)
            self.assert_business(repo.snapshot()["extractions"][0], origin="test-provider/test-model-snapshot")

        # Refusal, incompletion, invalid JSON/schema are terminal, without repair.
        for fixture, code in (("refusal", "refusal"), ("incomplete-valid-json", "incomplete"),
                              ("malformed", "invalid-json"), ("schema-not-object", "invalid-extraction")):
            with self.subTest(terminal=fixture):
                total = CTX.Value("i", 0)
                with Repository(self.directory / f"terminal-{fixture}.sqlite") as repo:
                    repo.enqueue([REQUESTS / "IT-001.json"], settings=ProcessingSettings())
                    Scheduler(repo, provider_factory=lambda _: CountedProvider(
                        MockProvider([MockOutcome(response_path=MOCKS / f"{fixture}.json")]), total)).run()
                    self.assertEqual(total.value, 1)
                    self.assert_error(repo.snapshot()["extractions"][0], code)

        # Exhaustion prints a retry command that works in a fresh interpreter
        # with the original source path and manifest changed after the failure.
        self.state = self.directory / "outage state.sqlite"
        source = self.directory / "original" / "IT-001.json"
        source.parent.mkdir()
        original_bytes = (REQUESTS / "IT-001.json").read_bytes()
        source.write_bytes(original_bytes)
        manifest = self.manifest({LEDGER["T02"]["message"]: "complete"})
        total = CTX.Value("i", 0)
        def unavailable(_):
            return CountedProvider(MockProvider([MockOutcome(error=ProviderUnavailable("scripted outage"))]), total)
        self.invoke(["intake", source, "--state", self.state, "--mock-map", manifest],
                    provider_factory=unavailable, scheduler_options={"base_delay": 0})
        self.assertEqual(total.value, 3)
        progress = self.fresh_cli(["progress", "--state", self.state])
        self.assertEqual(progress["counts"]["failures"], 1)
        self.assertEqual(progress["counts"]["backlog"], 0)
        failed = self.fresh_cli(["queue", "--state", self.state])["items"][0]
        self.assertEqual(failed["error"]["code"], "provider-unavailable")
        self.assertEqual(progress["items"][0]["call_count"], 3)
        source.write_bytes(b"changed source must not be read on saved-attempt retry")
        manifest.write_text("{}", encoding="utf-8")
        command = shlex.split(failed["retry_command"])
        self.assertEqual(command[:3], ["python3", "-m", "access_intake"])
        self.fresh_cli(command[3:])
        history = self.fresh_cli(["history", "--state", self.state, "--document", "IT-001.json"])
        self.assertEqual(history["total"], 2)
        self.assert_business(history["items"][-1])
        self.assertEqual(history["items"][-1]["source_sha256"], hashlib.sha256(original_bytes).hexdigest())
        self.assertNotEqual(history["items"][-1]["attempt_id"], failed["attempt_id"])
        self.assertEqual(self.fresh_cli(["resume", "--state", self.state])["scheduler"]["calls"], 0)

        # A hard total deadline terminates a held call and releases its worker.
        clock[0] = 100
        entered, release = CTX.Event(), CTX.Event()
        def expire_held_call(stage, job):
            if stage == "spawned" and job["document_id"] == "IT-001.json":
                self.assertTrue(entered.wait(5))
                clock[0] += 61
        with Repository(self.directory / "timeout.sqlite", clock=lambda: clock[0]) as repo:
            repo.enqueue([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"], settings=ProcessingSettings())
            Scheduler(repo, provider_factory=lambda _: HeldProvider(entered, release), workers=1,
                      clock=lambda: clock[0], hook=expire_held_call).run()
            current = repo.snapshot()["extractions"]
            self.assert_error(current[0], "attempt-timeout")
            self.assert_business(current[1], "T03")
            self.assertEqual(repo.progress()[0]["call_count"], 1)

        from access_intake.mock_config import provider_for_settings
        for size in (400, 2000):
            with self.subTest(messages=size):
                work = self.directory / f"batch-{size}"
                work.mkdir()
                mapping = {}
                for index in range(size):
                    case = "T02" if index % 2 == 0 else "T03"
                    old_id, new_id = LEDGER[case]["scalars"]["request_id"], f"IT-W{index:04d}"
                    text = LEDGER[case]["message"].replace(old_id, new_id)
                    (work / f"{new_id}.json").write_text(json.dumps({"message": text}), encoding="utf-8")
                    mapping[hashlib.sha256(text.encode("utf-8")).hexdigest()] = {
                        "response": str(MOCKS / f"{LEDGER[case]['fixture']}.json"), "substitutions": {old_id: new_id}}
                manifest.write_text(json.dumps(mapping), encoding="utf-8")
                state = self.directory / f"batch-{size}.sqlite"
                total, active, peak = CTX.Value("i", 0), CTX.Value("i", 0), CTX.Value("i", 0)
                ready, release = CTX.Event(), CTX.Event()
                spawned = [0]
                def observe_wave(stage, job):
                    if stage == "spawned":
                        spawned[0] += 1
                        if spawned[0] == 4:
                            self.assertTrue(ready.wait(5), "Four provider calls must be able to overlap")
                            release.set()
                def counted_factory(settings):
                    return CountedProvider(provider_for_settings(settings), total,
                                           active=active, peak=peak, ready=ready, release=release)
                report = self.invoke(["intake", work, "--state", state, "--mock-map", manifest, "--workers", "4"],
                                     provider_factory=counted_factory, hook=observe_wave)
                self.assertEqual(peak.value, 4)
                self.assertEqual(active.value, 0)
                self.assertEqual(total.value, size)
                self.assertEqual(report["scheduler"]["max_active"], 4)
                self.assertEqual(report["scheduler"]["calls"], size)
                progress = self.invoke(["progress", "--state", state, "--limit", "7"])
                self.assertEqual(progress["total"], size)
                self.assertEqual(len(progress["items"]), 7)
                self.assertEqual(progress["counts"]["completed"], size)
                self.assertEqual(progress["counts"]["backlog"], 0)
                self.assertEqual(progress["counts"]["failures"], 0)
                first = self.invoke(["queue", "--state", state, "--limit", "37"])
                second = self.invoke(["queue", "--state", state, "--limit", "37", "--offset", "37"])
                self.assertEqual(first["total"], size)
                self.assertEqual(len(first["items"]), 37)
                self.assertFalse({row["document_id"] for row in first["items"]} & {row["document_id"] for row in second["items"]})
                with Repository(state) as repo:
                    records = repo.snapshot()["extractions"]
                    self.assertEqual(len(records), size)
                    self.assertEqual(len({row["document_id"] for row in records}), size)
                    self.assertEqual(len(repo.progress()), size)
                    for index, record in enumerate(records):
                        case = "T02" if index % 2 == 0 else "T03"
                        identity = f"IT-W{index:04d}"
                        self.assertEqual(record["document_id"], identity + ".json")
                        self.assert_business(record, case, request_id=identity)
                        self.assertEqual(record["source_sha256"], hashlib.sha256((work / record["document_id"]).read_bytes()).hexdigest())
                resumed = self.invoke(["resume", "--state", state], provider_factory=Mock(side_effect=AssertionError("Completed work called provider")))
                self.assertEqual(resumed["scheduler"]["calls"], 0)
                self.assertEqual(self.invoke(["queue", "--state", state])["total"], size)
