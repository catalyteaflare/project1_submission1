"""T01 from planning/plan.md: provider playback, before routing or validation.

The fixture contains authored IT-001 facts from the plan's T02 source quotes.
T01's oracle is playback of the saved envelope, not extraction accuracy.
"""

import json
import os
import socket
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
COMPLETE = ROOT / "tests" / "mock_responses" / "complete.json"


class MockProviderContract(unittest.TestCase):
    def test_T01(self):
        """Replay once per call, including deterministic errors and held replies."""
        message = json.loads(
            (ROOT / "materials" / "requests" / "IT-001.json").read_text(
                encoding="utf-8"
            )
        )["message"]
        saved_envelope = json.loads(COMPLETE.read_text(encoding="utf-8"))
        original_getitem = type(os.environ).__getitem__

        def guarded_getitem(environ, key):
            if key == "API_KEY":
                raise AssertionError("T01 must not read API_KEY")
            return original_getitem(environ, key)

        with ExitStack() as guards:
            guards.enter_context(patch.object(type(os.environ), "__getitem__", guarded_getitem))
            for name in ("socket", "create_connection", "getaddrinfo"):
                guards.enter_context(patch.object(
                    socket, name, side_effect=AssertionError("T01 must not use the network")
                ))

            # Import here so the red phase reports the missing provider as T01.
            from access_intake.providers import MockOutcome, MockProvider, ProviderUnavailable

            now = [10.0]
            entered = Event()
            release = Event()
            unavailable = ProviderUnavailable("scripted outage")
            provider = MockProvider(
                outcomes=[
                    MockOutcome(response_path=COMPLETE),
                    MockOutcome(error=unavailable),
                    MockOutcome(response_path=COMPLETE, entered=entered, wait_for=release),
                ],
                clock=lambda: now[0],
            )

            with self.subTest("immediate saved envelope and mock provenance"):
                reply = provider.extract(message)
                self.assertEqual(reply, {**saved_envelope, "response_source": "mock"})
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(provider.calls[0].message, message)
                self.assertEqual(provider.calls[0].started_at, 10.0)
                self.assertEqual(provider.calls[0].completed_at, 10.0)

            now[0] = 20.0
            with self.subTest("scripted error is raised once and recorded"):
                with self.assertRaises(ProviderUnavailable) as caught:
                    provider.extract(message)
                self.assertIs(caught.exception, unavailable)
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(provider.calls[1].message, message)
                self.assertEqual(provider.calls[1].started_at, 20.0)
                self.assertEqual(provider.calls[1].completed_at, 20.0)

            now[0] = 30.0
            with self.subTest("event holds completion and injected clock records release"):
                with ThreadPoolExecutor(max_workers=1) as worker:
                    pending = worker.submit(provider.extract, message)
                    try:
                        self.assertTrue(entered.wait(timeout=2), "provider did not signal entry")
                        self.assertFalse(pending.done())
                        self.assertEqual(len(provider.calls), 3)
                        self.assertEqual(provider.calls[2].message, message)
                        self.assertEqual(provider.calls[2].started_at, 30.0)
                        self.assertIsNone(provider.calls[2].completed_at)
                        now[0] = 45.0
                    finally:
                        # Always unblock the worker, including after a failed assertion.
                        release.set()
                    delayed_reply = pending.result(timeout=2)
                self.assertEqual(delayed_reply, {**saved_envelope, "response_source": "mock"})
                self.assertEqual(provider.calls[2].completed_at, 45.0)
                self.assertEqual(len(provider.calls), 3)
