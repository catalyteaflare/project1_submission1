"""Injectable extraction providers and offline, scripted envelope playback."""

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Iterable, Protocol


class Provider(Protocol):
    """Return a provider envelope for a decoded source message."""

    def extract(self, message: str) -> dict[str, Any]: ...


class ProviderUnavailable(Exception):
    """The provider could not serve an extraction request."""


@dataclass(frozen=True)
class MockOutcome:
    """One saved reply or scripted exception, optionally held by an event.

    ``entered`` signals that the call has been recorded. ``wait_for`` controls
    when it may finish, without sleeps or a dependency on wall-clock time.
    """

    response_path: Path | None = None
    error: Exception | None = None
    entered: Event | None = None
    wait_for: Event | None = None

    def __post_init__(self) -> None:
        if (self.response_path is None) == (self.error is None):
            raise ValueError("A mock outcome needs exactly one response_path or error")


@dataclass
class ProviderCall:
    message: str
    started_at: float
    completed_at: float | None = None


class MockProvider:
    """Consume scripted outcomes in call order without credentials or network.

    Only script selection and call recording hold the lock. A held reply leaves
    other callers free to consume subsequent outcomes. Each playback reads a
    fresh envelope so a caller's changes cannot affect a later reply.
    """

    def __init__(
        self,
        outcomes: Iterable[MockOutcome],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._outcomes = deque(outcomes)
        self._clock = clock
        self._lock = Lock()
        self.calls: list[ProviderCall] = []

    def extract(self, message: str) -> dict[str, Any]:
        with self._lock:
            if not self._outcomes:
                raise RuntimeError("Mock provider script exhausted; add a saved response outcome")
            outcome = self._outcomes.popleft()
            call = ProviderCall(message=message, started_at=self._clock())
            self.calls.append(call)

        try:
            if outcome.entered is not None:
                outcome.entered.set()
            if outcome.wait_for is not None:
                outcome.wait_for.wait()
            if outcome.error is not None:
                raise outcome.error
            envelope = json.loads(Path(outcome.response_path).read_text(encoding="utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("Mock response fixture must contain a JSON envelope object")
            return {**envelope, "response_source": "mock"}
        finally:
            with self._lock:
                call.completed_at = self._clock()
