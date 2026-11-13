"""Offline test harness; all provider replies come from saved fixtures."""

import json
import os
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from access_intake.providers import MockOutcome, MockProvider

ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
REQUESTS = ROOT / "materials" / "requests"


def saved(name):
    return json.loads((MOCKS / f"{name}.json").read_text(encoding="utf-8"))


def message(name="IT-001.json"):
    return json.loads((REQUESTS / name).read_text(encoding="utf-8"))["message"]


def playback(*names):
    return MockProvider([MockOutcome(response_path=MOCKS / f"{name}.json") for name in names])


class OfflineTest(unittest.TestCase):
    def setUp(self):
        original = type(os.environ).__getitem__

        def guard(environ, key):
            if key == "API_KEY":
                raise AssertionError("Test must not read API_KEY")
            return original(environ, key)

        self.key_guard = patch.object(type(os.environ), "__getitem__", guard)
        self.key_guard.start()
        self.addCleanup(self.key_guard.stop)
        for name in ("socket", "create_connection", "getaddrinfo"):
            patcher = patch.object(socket, name, side_effect=AssertionError("Network denied in tests"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_error(self, record, code):
        self.assertIsNone(record["extraction"])
        self.assertEqual((record["status"], record["owner"], record["next_action"]),
                         ("error", "IT Service Desk", "retry-extraction"))
        self.assertEqual(record["error"]["code"], code)
        self.assertTrue(record["error"]["message"])
