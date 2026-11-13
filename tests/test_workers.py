"""T36–T38: single-host process coordination, recovery and export consistency.

Use saved replies, process barriers, shared logical clocks and injected crash
points. No sleeps decide test ordering. POSIX fork inherits the network/key
guards into worker processes; other platforms retain the earlier unit tests.
"""

import hashlib
import json
import multiprocessing
import os
import socket
import sqlite3
import unittest
from multiprocessing.connection import wait
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from access_intake.providers import MockOutcome, MockProvider, ProviderUnavailable


ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "materials" / "requests"
MOCKS = ROOT / "tests" / "mock_responses"
LEDGER = json.loads((ROOT / "tests/expected_facts.json").read_text(encoding="utf-8"))
CTX = multiprocessing.get_context("fork") if "fork" in multiprocessing.get_all_start_methods() else None


def playback(name, *, entered=None, release=None):
    return MockProvider([MockOutcome(response_path=MOCKS / f"{name}.json", entered=entered, wait_for=release)])


def worker(state, attempt_id, clock, sender, *, entered=None, release=None,
           start=None, ready=None, peer_id=None, crash_at=None):
    from access_intake.repository import Repository

    def fault(stage, identity):
        if stage == crash_at:
            os._exit(17)

    try:
        with Repository(state, clock=lambda: clock.value, fault_hook=fault) as repo:
            if ready is not None:
                ready.set()
            if start is not None and not start.wait(5):
                raise AssertionError("Worker start barrier was not released")
            provider = playback("complete", entered=entered, release=release)
            result = repo.run_one(attempt_id, provider=provider, lease_seconds=10)
            peer_provider = playback("production-write")
            peer = repo.run_one(peer_id, provider=peer_provider, lease_seconds=10) if peer_id is not None else None
            sender.send({"result": result, "primary_calls": len(provider.calls),
                         "peer": peer, "peer_calls": len(peer_provider.calls)})
    except BaseException as error:
        sender.send({"worker_error": type(error).__name__})
    finally:
        sender.close()


def export_worker(state, destination, crash_at):
    from access_intake.repository import Repository
    from access_intake.snapshots import export_generation

    def fault(stage, identity):
        if stage == crash_at:
            os._exit(18)

    with Repository(state) as repo:
        export_generation(repo, destination, fault_hook=fault)


@unittest.skipUnless(CTX is not None, "Process-barrier tests require POSIX fork")
class WorkerContract(unittest.TestCase):
    def setUp(self):
        original_getitem = type(os.environ).__getitem__

        def guarded_getitem(environ, key):
            if key == "API_KEY":
                raise AssertionError("Worker tests must not read API_KEY")
            return original_getitem(environ, key)

        key_guard = patch.object(type(os.environ), "__getitem__", guarded_getitem)
        key_guard.start()
        self.addCleanup(key_guard.stop)
        for name in ("socket", "create_connection", "getaddrinfo"):
            guard = patch.object(socket, name, side_effect=AssertionError("Network denied in worker tests"))
            guard.start()
            self.addCleanup(guard.stop)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def launch(self, state, identity, clock, **options):
        reader, sender = CTX.Pipe(duplex=False)
        child = CTX.Process(target=worker, args=(state, identity, clock, sender), kwargs=options)
        child.start()
        sender.close()

        def cleanup():
            # Never set an event after killing a waiter: its lock may be abandoned.
            if child.is_alive():
                child.terminate()
            child.join(5)
            if child.is_alive():
                child.kill()
                child.join(5)
            reader.close()

        self.addCleanup(cleanup)
        return child, reader

    def receive(self, child, reader):
        self.assertTrue(reader.poll(5), "Worker did not return a result")
        result = reader.recv()  # Drain large messages before joining their sender.
        self.assertNotIn("worker_error", result, result)
        child.join(5)
        self.assertEqual(child.exitcode, 0)
        return result

    def assert_business(self, record, case="T02", *, end=None):
        expected = LEDGER[case]
        self.assertIsNone(record["error"], record)
        self.assertEqual(record["document_id"], expected["document_id"])
        self.assertEqual(record["response_source"], "mock")
        self.assertEqual([record[name] for name in ("status", "owner", "next_action")], expected["route"])
        for field, value in expected["scalars"].items():
            self.assertEqual(record["extraction"][field], end if field == "end_date" and end is not None else value, field)

    def assert_snapshot(self, snapshot):
        records = {record["document_id"]: record for record in snapshot["extractions"]}
        self.assertEqual(len(records), len(snapshot["extractions"]))
        self.assertEqual(len(snapshot["queue"]), len(records))
        self.assertEqual({row["document_id"] for row in snapshot["queue"]}, set(records))
        for queued in snapshot["queue"]:
            record = records[queued["document_id"]]
            for field in ("attempt_id", "source_sha256", "revision", "status", "owner", "next_action"):
                self.assertEqual(queued[field], record[field])

    def corrected_source(self):
        source = self.directory / "IT-001.json"
        source.write_text(json.dumps({
            "message": LEDGER["T02"]["message"] + " Correction: replace the end date with 2026-11-07.",
            "exported_at": "2000-01-01T00:00:00Z",
        }), encoding="utf-8")
        return source

    def test_T36(self):
        from access_intake.repository import Repository, ProcessingSettings

        state = self.directory / "overlap.sqlite"
        with Repository(state) as repo:
            identities = repo.enqueue([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"], settings=ProcessingSettings())
        clock = CTX.Value("d", 100)
        start, entered, release = CTX.Event(), CTX.Event(), CTX.Event()
        ready = [CTX.Event(), CTX.Event()]
        workers = [self.launch(state, identities[0], clock, entered=entered, release=release,
                               start=start, ready=signal, peer_id=identities[1]) for signal in ready]
        self.assertTrue(all(signal.wait(5) for signal in ready))
        start.set()
        try:
            self.assertTrue(entered.wait(5))
            available = wait([reader for _, reader in workers], timeout=5)
            self.assertEqual(len(available), 1, "One claimant loses while the winner remains held")
            loser_index = next(i for i, (_, reader) in enumerate(workers) if reader is available[0])
            loser = self.receive(*workers[loser_index])
            self.assertIsNone(loser["result"])
            self.assertEqual(loser["primary_calls"], 0)
            self.assertEqual(loser["peer_calls"], 1)
            self.assert_business(loser["peer"], "T03")
            with Repository(state, clock=lambda: clock.value) as repo:
                snapshot = repo.snapshot()
                self.assert_snapshot(snapshot)
                self.assertEqual([row["document_id"] for row in snapshot["extractions"]], ["IT-002.json"])
                pending = {row["document_id"]: row for row in repo.progress()}
                self.assertEqual(pending["IT-001.json"]["work_state"], "running")
        finally:
            release.set()
        winner = self.receive(*workers[1 - loser_index])
        self.assertEqual(winner["primary_calls"], 1)
        self.assertEqual(winner["peer_calls"], 0)
        self.assert_business(winner["result"])
        with Repository(state) as repo:
            self.assert_snapshot(repo.snapshot())
            self.assertEqual(len(repo.snapshot()["extractions"]), 2)
            self.assertEqual(len(repo.history("IT-001.json")), 1)
            self.assertEqual(len(repo.history("IT-002.json")), 1)

        # Recover an expired claimant, then release its late response.
        state = self.directory / "lease.sqlite"
        with Repository(state) as repo:
            old_id = repo.enqueue([REQUESTS / "IT-001.json"], settings=ProcessingSettings())[0]
        entered, release = CTX.Event(), CTX.Event()
        held = self.launch(state, old_id, clock, entered=entered, release=release)
        try:
            self.assertTrue(entered.wait(5))
            clock.value = 111
            with Repository(state, clock=lambda: clock.value) as repo:
                recovered = repo.recover()
                self.assertEqual(len(recovered), 1)
                self.assertEqual(repo.recover(), [], "A recovered lease must not be cloned twice")
                states = {row["attempt_id"]: row["work_state"] for row in repo.progress()}
                self.assertEqual(states[old_id], "interrupted")
                self.assertEqual(states[recovered[0]], "queued")
                replacement = repo.run_one(recovered[0], provider=playback("complete"), lease_seconds=10)
                self.assert_business(replacement)
        finally:
            release.set()
        late = self.receive(*held)["result"]
        self.assert_business(late)
        self.assertEqual(late["attempt_id"], old_id)
        with Repository(state) as repo:
            self.assertEqual(repo.snapshot()["extractions"], [replacement])
            self.assertEqual({row["attempt_id"] for row in repo.history("IT-001.json")}, {old_id, replacement["attempt_id"]})

        # A wrong token or repeated completion cannot publish or duplicate history.
        state = self.directory / "tokens.sqlite"
        with Repository(state, clock=lambda: 100) as repo:
            identity = repo.enqueue([REQUESTS / "IT-001.json"], settings=ProcessingSettings())[0]
            claim = repo.claim(identity, lease_seconds=10)
            self.assertTrue(claim["lease_token"])
            result = repo.compute(claim, provider=playback("complete"))
            with self.assertRaises(ValueError):
                repo.finish({**claim, "lease_token": "not-the-current-token"}, result)
            self.assertEqual(repo.snapshot()["extractions"], [])
            repo.finish(claim, result)
            with self.assertRaises(ValueError):
                repo.finish(claim, result)
            self.assertEqual(len(repo.history("IT-001.json")), 1)

        # A configured zero lock wait fails immediately rather than hanging.
        with Repository(self.directory / "locks.sqlite", lock_timeout=0) as repo:
            with sqlite3.connect(repo.path) as writer:
                writer.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    repo.enqueue([REQUESTS / "IT-001.json"], settings=ProcessingSettings())
                writer.rollback()

    def test_T37(self):
        from access_intake.repository import Repository, ProcessingSettings
        from access_intake.snapshots import export_generation, read_snapshot

        for crash_at in ("after_claim", "after_response", "after_commit"):
            with self.subTest(crash=crash_at):
                state = self.directory / f"{crash_at}.sqlite"
                clock = CTX.Value("d", 100)
                with Repository(state) as repo:
                    ids = repo.enqueue([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"], settings=ProcessingSettings())
                    peer = repo.run_one(ids[1], provider=playback("production-write"))
                child, _ = self.launch(state, ids[0], clock, crash_at=crash_at)
                child.join(5)
                self.assertEqual(child.exitcode, 17)
                clock.value = 1000
                with Repository(state, clock=lambda: clock.value) as repo:
                    before = repo.snapshot()
                    self.assert_snapshot(before)
                    if crash_at == "after_commit":
                        self.assertEqual(len(before["extractions"]), 2)
                        self.assertEqual(repo.recover(), [])
                    else:
                        self.assertEqual(before["extractions"], [peer])
                        self.assertEqual(repo.history("IT-001.json"), [])
                        pending = next(row for row in repo.progress() if row["attempt_id"] == ids[0])
                        self.assertEqual(pending["work_state"], "running", "Progress reports the stored state until recovery")
                        replacements = repo.recover()
                        self.assertEqual(len(replacements), 1)
                        repo.run_one(replacements[0], provider=playback("complete"))
                    no_calls = playback("complete")
                    self.assertIsNone(repo.run_one(provider=no_calls))
                    self.assertEqual(no_calls.calls, [], "Resume must not reprocess committed successes")
                    snapshot = repo.snapshot()
                    self.assert_snapshot(snapshot)
                    self.assert_business(snapshot["extractions"][0])
                    self.assert_business(snapshot["extractions"][1], "T03")
                    self.assertEqual(repo.history("IT-002.json"), [peer])

        state, destination = self.directory / "exports.sqlite", self.directory / "exports"
        with Repository(state) as repo:
            repo.intake([REQUESTS / "IT-001.json"], provider=playback("complete"), settings=ProcessingSettings())
            export_generation(repo, destination)
            original = read_snapshot(destination)
            repo.intake([REQUESTS / "IT-002.json"], provider=playback("production-write"), settings=ProcessingSettings())
        for crash_at in ("after_generation", "after_manifest"):
            child = CTX.Process(target=export_worker, args=(state, destination, crash_at))
            child.start()
            self.addCleanup(child.close)
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join(5)
            self.assertEqual(child.exitcode, 18)
            current = read_snapshot(destination)
            self.assert_snapshot(current)
            if crash_at == "after_generation":
                self.assertEqual(current, original, "An abandoned generation cannot replace the active pair")
            else:
                self.assertEqual(len(current["extractions"]), 2)
                self.assert_business(current["extractions"][0])
                self.assert_business(current["extractions"][1], "T03")

        # Publish between reading the extraction and queue files. One reader must
        # retain its originally resolved generation for both files.
        frozen = read_snapshot(destination)
        with Repository(state) as repo:
            revised = repo.intake([self.corrected_source()], provider=playback("changed-end"), settings=ProcessingSettings())[0]
            published = []
            def publish_during_read(stage, generation):
                if stage == "after_extractions_read":
                    published.append(export_generation(repo, destination))
            observed = read_snapshot(destination, fault_hook=publish_during_read)
            self.assertEqual(len(published), 1)
            self.assertEqual(observed, frozen)
        fresh = read_snapshot(destination)
        self.assert_snapshot(fresh)
        self.assertEqual(fresh["extractions"][0], revised)

    def test_T38(self):
        from access_intake.repository import Repository, ProcessingSettings

        # Missing enqueue must fail before launching or waiting on a child.
        with Repository(self.directory / "preflight.sqlite") as repo:
            self.assertTrue(callable(repo.enqueue))
        for newer_fails in (False, True):
            with self.subTest(newer_fails=newer_fails):
                state = self.directory / f"reverse-{newer_fails}.sqlite"
                clock = CTX.Value("d", 100)
                with Repository(state) as repo:
                    old_id = repo.enqueue([REQUESTS / "IT-001.json"], settings=ProcessingSettings())[0]
                entered, release = CTX.Event(), CTX.Event()
                held = self.launch(state, old_id, clock, entered=entered, release=release)
                source = self.corrected_source()
                try:
                    self.assertTrue(entered.wait(5))
                    provider = (MockProvider([MockOutcome(error=ProviderUnavailable("scripted outage"))])
                                if newer_fails else playback("changed-end"))
                    with Repository(state, clock=lambda: clock.value) as repo:
                        newer = repo.intake([source], provider=provider, settings=ProcessingSettings())[0]
                finally:
                    release.set()
                older = self.receive(*held)["result"]
                self.assert_business(older)
                self.assertGreater(newer["revision"], older["revision"])
                self.assertEqual(newer["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
                with Repository(state) as repo:
                    self.assertEqual(repo.snapshot()["extractions"], [newer])
                    self.assert_snapshot(repo.snapshot())
                    self.assertEqual(repo.history("IT-001.json"), [older, newer])
                if newer_fails:
                    self.assertIsNone(newer["extraction"])
                    self.assertEqual((newer["status"], newer["owner"], newer["next_action"]),
                                     ("error", "IT Service Desk", "retry-extraction"))
                    self.assertEqual(newer["error"]["code"], "provider-unavailable")
                else:
                    self.assert_business(newer, end="2026-11-07")
