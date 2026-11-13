"""Explicit saved-envelope configuration for offline operator commands."""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from .http_provider import HttpProvider, ProviderFailure
from .inputs import InputError, decode_export


DEFAULT_MOCK_MAP = Path(__file__).resolve().parents[1] / "config" / "mock-map.json"
MAX_MOCK_BYTES = 1_048_576


class MockConfiguration:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.entries = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(self.entries, dict):
            raise ValueError("Mock map must be an object keyed by decoded-message SHA-256")
        self._fixtures = {}

    def settings_for(self, path, raw, settings):
        try:
            source = decode_export(Path(path), raw)
        except InputError:
            return settings
        entry = self.entries.get(hashlib.sha256(source.message.encode("utf-8")).hexdigest())
        if entry is None:
            return replace(settings, request_settings={})
        if not isinstance(entry, dict) or not isinstance(entry.get("response"), str):
            raise ValueError("Each mock-map entry needs a response filename")
        substitutions = entry.get("substitutions", {})
        if not isinstance(substitutions, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in substitutions.items()):
            raise ValueError("Mock substitutions must map nonempty strings to strings")
        fixture = (self.path.parent / entry["response"]).resolve()
        if fixture not in self._fixtures:
            with fixture.open("rb") as reply:
                contents = reply.read(MAX_MOCK_BYTES + 1)
            if len(contents) > MAX_MOCK_BYTES:
                raise ValueError("Saved mock response exceeds the 1 MiB limit")
            self._fixtures[fixture] = {"path": str(fixture), "sha256": hashlib.sha256(contents).hexdigest()}
        return replace(settings, request_settings={"mock_response": {
            **self._fixtures[fixture], "substitutions": dict(substitutions),
        }})


class SavedProvider:
    response_source = "mock"

    def __init__(self, settings):
        self.settings = settings

    def processing_configuration(self):
        return {
            "provider": self.settings.provider, "model": self.settings.model,
            "endpoint": self.settings.endpoint, "request_settings": copy.deepcopy(self.settings.request_settings),
            "prompt": self.settings.prompt, "schema": copy.deepcopy(self.settings.schema),
        }

    def extract(self, message):
        entry = self.settings.request_settings.get("mock_response")
        try:
            if not entry:
                raise ValueError("No saved reply")
            with Path(entry["path"]).open("rb") as reply:
                raw = reply.read(MAX_MOCK_BYTES + 1)
            if len(raw) > MAX_MOCK_BYTES or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("Saved reply changed")
            text = raw.decode("utf-8")
            for old, new in entry.get("substitutions", {}).items():
                text = text.replace(old, new)
            if len(text.encode("utf-8")) > MAX_MOCK_BYTES:
                raise ValueError("Expanded reply too large")
            envelope = json.loads(text)
            if not isinstance(envelope, dict):
                raise ValueError("Saved reply must be an envelope object")
        except (OSError, ValueError, KeyError, TypeError):
            raise ProviderFailure("mock-response", "Saved mock reply is missing, changed or invalid; refresh with a valid --mock-map") from None
        return {**envelope, "response_source": "mock"}


def provider_for_settings(settings):
    if settings.provider == "mock":
        return SavedProvider(settings)
    return HttpProvider.from_settings(settings)
