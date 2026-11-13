"""Bounded POSIX workers, durable backoff and total-attempt deadlines."""

import json
import math
import multiprocessing
import random
import signal
import threading
from multiprocessing.connection import wait as wait_connections
from pathlib import Path
from time import sleep

from .http_provider import ProviderFailure
from .inputs import InputError, decode_export
from .repository import ProcessingSettings, Repository
from .providers import ProviderUnavailable
from .validation import error_record


def retry_delay(error, calls, *, jitter=random.random, base_delay=0.5):
    requested = getattr(error, "retry_after", None)
    if requested is not None and math.isfinite(requested):
        return min(60, max(0, requested))
    base = min(10, base_delay * 2 ** max(0, calls - 1))
    return min(10, base * (1 + min(1, max(0, jitter())) * 0.25))


def _compute(path, job, provider, sender, remaining):
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        with Repository(path) as repo:
            result = repo.compute(job, provider=provider, raise_provider_errors=True, call_recorded=True)
        sender.send(("result", result))
    except (ProviderUnavailable, TimeoutError) as error:
        sender.send(("failure", {
            "code": getattr(error, "code", "provider-timeout" if isinstance(error, TimeoutError) else "provider-unavailable"),
            "retryable": getattr(error, "retryable", True), "retry_after": getattr(error, "retry_after", None),
        }))
    except BaseException:
        sender.send(("failure", {"code": "worker-error", "retryable": False}))
    finally:
        sender.close()


class Scheduler:
    def __init__(self, repository, *, provider_factory, workers=4, timeout=60, max_calls=3,
                 clock=None, wait=sleep, jitter=random.random, base_delay=0.5, hook=None):
        if (not 1 <= workers <= 32 or not math.isfinite(timeout) or timeout <= 0
                or not 1 <= max_calls <= 3 or not math.isfinite(base_delay) or base_delay < 0):
            raise ValueError("Use 1–32 workers, a positive finite timeout, 1–3 calls and nonnegative backoff")
        if "fork" not in multiprocessing.get_all_start_methods():
            raise ValueError("The worker scheduler requires a POSIX host with fork")
        self.context = multiprocessing.get_context("fork")
        self.repository, self.factory = repository, provider_factory
        self.workers, self.timeout, self.max_calls = workers, timeout, max_calls
        self.clock, self.wait, self.jitter = clock or repository.clock, wait, jitter
        self.base_delay = base_delay
        self.hook = hook or (lambda stage, job: None)

    @staticmethod
    def _needs_provider(job):
        if job["kind"] != "extract":
            return False
        try:
            decode_export(Path(job["document_id"]), job["source_bytes"])
        except InputError:
            return False
        return True

    def _failure(self, job, details):
        error = ProviderFailure(details["code"], "Provider call failed", retryable=details["retryable"],
                                retry_after=details.get("retry_after"))
        if error.retryable and job["call_count"] < self.max_calls:
            delay = retry_delay(error, job["call_count"], jitter=self.jitter, base_delay=self.base_delay)
            if self.clock() + delay < job["deadline"] and self.repository.defer(job, seconds=delay, code=error.code):
                self.hook("retry-wait", job)
                return
        settings = ProcessingSettings(**json.loads(job["settings_json"]))
        origin = "mock" if settings.provider == "mock" else f"{settings.provider}/{settings.model}"
        result = self.repository.decorate(job, error_record(error.code, "Extraction failed or timed out; retry this saved attempt", origin))
        self.repository.finish(job, result)
        self.hook("completed", job)

    @staticmethod
    def _stop(process, *, terminate=False):
        if process.pid is None:
            return
        if terminate and process.is_alive():
            process.terminate()
        process.join(0.5)
        if process.is_alive():
            process.kill()
            process.join(1)

    def run(self):
        self.repository.recover()
        active, pending_claim = {}, None
        metrics = {"max_active": 0, "calls": 0}
        old_handler = None
        if threading.current_thread() is threading.main_thread():
            old_handler = signal.getsignal(signal.SIGTERM)
            def interrupted(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, interrupted)
        try:
            while True:
                while len(active) < self.workers:
                    job = self.repository.claim(lease_seconds=self.timeout + 5)
                    if job is None:
                        break
                    pending_claim = job
                    needs_provider = self._needs_provider(job)
                    exhausted = needs_provider and job["call_count"] >= self.max_calls
                    expired = job["deadline"] is not None and self.clock() >= job["deadline"]
                    provider = None
                    configuration_failed = False
                    if needs_provider and not exhausted and not expired:
                        try:
                            provider = self.factory(ProcessingSettings(**json.loads(job["settings_json"])))
                        except (ValueError, OSError, KeyError, ProviderUnavailable):
                            configuration_failed = True
                    job = self.repository.prepare_execution(job, timeout=self.timeout,
                        call_provider=needs_provider and not (exhausted or expired or configuration_failed))
                    pending_claim = job
                    if self.clock() >= job["deadline"] or exhausted or configuration_failed:
                        code = ("attempt-timeout" if self.clock() >= job["deadline"] else
                                "provider-configuration" if configuration_failed else job["last_error_code"] or "retry-limit")
                        self._failure(job, {"code": code, "retryable": False})
                        pending_claim = None
                        continue
                    receiver, sender = self.context.Pipe(duplex=False)
                    process = self.context.Process(target=_compute,
                        args=(self.repository.path, job, provider, sender, max(0.001, job["deadline"] - self.clock())))
                    active[receiver] = (process, job)
                    pending_claim = None
                    try:
                        process.start()
                    finally:
                        sender.close()
                    metrics["calls"] += int(needs_provider)
                    metrics["max_active"] = max(metrics["max_active"], len(active))
                    self.hook("spawned", job)
                if not active:
                    ready_at = self.repository.next_ready_at()
                    if ready_at is None:
                        break
                    self.wait(max(0, ready_at - self.clock()))
                    continue
                ready = wait_connections(list(active), timeout=0.05)
                for connection in list(active):
                    process, job = active[connection]
                    expired = self.clock() >= job["deadline"]
                    if connection not in ready and not expired:
                        continue
                    if expired:
                        kind, payload = "failure", {"code": "attempt-timeout", "retryable": False}
                    else:
                        try:
                            kind, payload = connection.recv()
                        except (EOFError, OSError):
                            kind, payload = "failure", {"code": "worker-exited", "retryable": True}
                    self._stop(process, terminate=expired)
                    if process.exitcode == -signal.SIGALRM:
                        kind, payload = "failure", {"code": "attempt-timeout", "retryable": False}
                    connection.close()
                    del active[connection]
                    if kind == "result":
                        self.repository.finish(job, payload)
                        self.hook("completed", job)
                    else:
                        self._failure(job, payload)
        finally:
            for connection, (process, job) in active.items():
                self._stop(process, terminate=True)
                connection.close()
                self.repository.interrupt(job)
            if pending_claim is not None:
                self.repository.interrupt(pending_claim)
            if old_handler is not None:
                signal.signal(signal.SIGTERM, old_handler)
        return metrics
