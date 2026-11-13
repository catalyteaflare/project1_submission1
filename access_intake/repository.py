"""Durable attempts, current results, history, reuse and configuration refresh.

Provider execution happens outside write transactions. This step's service
executes sequentially; multi-process leases and recovery are step 7.
"""

import copy
import hashlib
import json
import shlex
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from typing import Any, Callable, Iterable
from uuid import uuid4

from .inputs import InputError, batch_paths, decode_export
from .pipeline import process_source
from .policy import POLICY_VERSION, route
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT
from .providers import Provider, ProviderUnavailable
from .validation import SCHEMA, error_record


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _no_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for name, item in value.items():
            normalized = name.lower().replace("-", "_")
            if any(word in normalized for word in ("api_key", "authorization", "credential", "password", "secret", "access_token")):
                raise ValueError("Processing settings must not contain credential fields")
            _no_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _no_credentials(item)


@dataclass(frozen=True)
class ProcessingSettings:
    """Serializable producing settings, with routing policy versioned separately."""

    provider: str = "mock"
    model: str = "saved-envelope"
    endpoint: str | None = None
    request_settings: dict = field(default_factory=dict)
    prompt: str = SYSTEM_PROMPT
    schema: dict = field(default_factory=lambda: copy.deepcopy(SCHEMA))
    extraction_version: str = PROMPT_VERSION
    policy_version: str = POLICY_VERSION

    def __post_init__(self):
        settings = asdict(self)
        _no_credentials(settings)
        _json(settings)

    def extraction_fingerprint(self, raw: bytes) -> str:
        producing = asdict(self)
        del producing["policy_version"]
        identity = {"source_sha256": hashlib.sha256(raw).hexdigest(), "settings": producing}
        return hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()

    @classmethod
    def from_provider(cls, provider, *, policy_version=POLICY_VERSION, extraction_version=PROMPT_VERSION):
        return cls(**provider.processing_configuration(), policy_version=policy_version,
                   extraction_version=extraction_version)

    def check_provider(self, provider):
        if hasattr(provider, "processing_configuration"):
            expected = asdict(self)
            actual = provider.processing_configuration()
            if any(expected[name] != value for name, value in actual.items()):
                raise ValueError("Provider configuration differs from the saved producing settings")


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                latest_attempt_id TEXT,
                current_attempt_id TEXT
            );
            CREATE TABLE IF NOT EXISTS attempts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                revision INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_bytes BLOB NOT NULL,
                settings_json TEXT NOT NULL,
                extraction_fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                parent_attempt_id TEXT,
                work_state TEXT NOT NULL,
                accepted_at REAL NOT NULL,
                completed_at REAL,
                result_json TEXT
            );
            CREATE INDEX IF NOT EXISTS attempt_document ON attempts(document_id, sequence);
        """)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _transaction(self, *, write=False):
        self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _attempt(self, attempt_id):
        row = self.connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown attempt {attempt_id}")
        return row

    @staticmethod
    def _pending(row):
        return {
            "document_id": row["document_id"], "attempt_id": row["attempt_id"],
            "revision": row["revision"], "source_sha256": row["source_sha256"],
            "work_state": row["work_state"], "settings": json.loads(row["settings_json"]),
        }

    def saved_attempt(self, attempt_id: str):
        row = self._attempt(attempt_id)
        return {**self._pending(row), "source_bytes": row["source_bytes"]}

    def _schedule(self, document_id, raw, settings, *, force=False, revision=None):
        # Freeze nested dictionaries before computing or saving their identity.
        settings = ProcessingSettings(**json.loads(_json(asdict(settings))))
        source_hash = hashlib.sha256(raw).hexdigest()
        fingerprint = settings.extraction_fingerprint(raw)
        with self._transaction(write=True):
            document = self.connection.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
            if document is None:
                accepted_revision = 1
                self.connection.execute("INSERT INTO documents(document_id, revision, source_sha256) VALUES (?,?,?)",
                                        (document_id, accepted_revision, source_hash))
            elif revision is not None:
                accepted_revision = revision
            else:
                accepted_revision = document["revision"] + (document["source_sha256"] != source_hash)

            kind, parent_id = "extract", None
            if document and document["latest_attempt_id"] and not force:
                latest = self._attempt(document["latest_attempt_id"])
                if latest["extraction_fingerprint"] == fingerprint:
                    prior = json.loads(latest["result_json"]) if latest["result_json"] else None
                    same_policy = json.loads(latest["settings_json"])["policy_version"] == settings.policy_version
                    # Terminal errors remain reusable; rerouting cannot create facts.
                    if same_policy or (prior is not None and prior["error"] is not None):
                        return None, prior if prior is not None else self._pending(latest)
                    if prior is not None and prior["extraction"] is not None:
                        kind, parent_id = "reroute", latest["attempt_id"]

            attempt_id = uuid4().hex
            self.connection.execute("""INSERT INTO attempts(attempt_id, document_id, revision, source_sha256,
                source_bytes, settings_json, extraction_fingerprint, kind, parent_attempt_id, work_state, accepted_at)
                VALUES (?,?,?,?,?,?,?,?,?,'queued',?)""",
                (attempt_id, document_id, accepted_revision, source_hash, raw, _json(asdict(settings)),
                 fingerprint, kind, parent_id, time()))
            if document is None or accepted_revision >= document["revision"]:
                self.connection.execute("UPDATE documents SET revision=?, source_sha256=?, latest_attempt_id=? WHERE document_id=?",
                                        (accepted_revision, source_hash, attempt_id, document_id))
        return attempt_id, None

    def _execute(self, attempt_id, *, provider=None, provider_factory=None, router=route):
        with self._transaction(write=True):
            job = self._attempt(attempt_id)
            if job["work_state"] != "queued":
                raise ValueError("Only queued work can execute")
            self.connection.execute("UPDATE attempts SET work_state='running' WHERE attempt_id=?", (attempt_id,))
        settings = ProcessingSettings(**json.loads(job["settings_json"]))
        parent = None
        try:
            document = decode_export(Path(job["document_id"]), job["source_bytes"])
        except InputError as error:
            result = error_record("input-error", str(error), "not-called")
        else:
            if job["kind"] == "reroute":
                parent = json.loads(self._attempt(job["parent_attempt_id"])["result_json"])
                result = {**router(parent["extraction"], document.message, document.document_id),
                          "error": None, "response_source": parent["response_source"]}
            else:
                try:
                    if provider_factory is not None:
                        provider = provider_factory(settings)
                    if provider is None:
                        raise ValueError("An injected provider or provider factory is required")
                    settings.check_provider(provider)
                except (ValueError, OSError, ProviderUnavailable):
                    origin = "mock" if settings.provider == "mock" else f"{settings.provider}/{settings.model}"
                    result = error_record("provider-configuration", "Cannot construct the saved provider configuration; inspect settings and retry", origin)
                else:
                    result = process_source(document, provider)
        result.update(
            document_id=job["document_id"], source_sha256=job["source_sha256"],
            attempt_id=attempt_id, revision=job["revision"], settings=asdict(settings),
            policy_version=settings.policy_version, extraction_fingerprint=job["extraction_fingerprint"],
            attempt_kind=job["kind"],
        )
        result["extraction_settings"] = (parent["extraction_settings"] if parent is not None else
                                         asdict(settings) if result["extraction"] is not None else None)
        result["extraction_attempt_id"] = (parent["extraction_attempt_id"] if parent is not None else
                                           attempt_id if result["extraction"] is not None else None)
        if result["error"]:
            result["retry_command"] = f"python3 -m access_intake retry --attempt {attempt_id} --state {shlex.quote(str(self.path))}"
        # Attempt completion/history and current-result publication share a commit.
        with self._transaction(write=True):
            self.connection.execute("UPDATE attempts SET work_state='completed', completed_at=?, result_json=? WHERE attempt_id=?",
                                    (time(), _json(result), attempt_id))
            self.connection.execute("UPDATE documents SET current_attempt_id=? WHERE document_id=? AND latest_attempt_id=?",
                                    (attempt_id, job["document_id"], attempt_id))
        return result

    def intake(self, paths: Iterable[Path], *, provider: Provider, settings: ProcessingSettings):
        settings.check_provider(provider)
        results = []
        for path in batch_paths(paths):
            attempt_id, reused = self._schedule(path.name, path.read_bytes(), settings)
            results.append(reused if attempt_id is None else self._execute(attempt_id, provider=provider))
        return results

    def retry(self, attempt_id: str, *, provider_factory: Callable):
        saved = self._attempt(attempt_id)
        settings = ProcessingSettings(**json.loads(saved["settings_json"]))
        new_id, _ = self._schedule(saved["document_id"], saved["source_bytes"], settings,
                                   force=True, revision=saved["revision"])
        return self._execute(new_id, provider_factory=provider_factory)

    def refresh(self, *, settings: ProcessingSettings, provider_factory: Callable, router: Callable = route):
        rows = self.connection.execute("""SELECT a.* FROM documents d JOIN attempts a
            ON a.attempt_id=d.latest_attempt_id ORDER BY d.document_id""").fetchall()
        results = []
        for saved in rows:
            attempt_id, _ = self._schedule(saved["document_id"], saved["source_bytes"], settings)
            if attempt_id is not None:
                results.append(self._execute(attempt_id, provider_factory=provider_factory, router=router))
        return results

    def history(self, document_id: str):
        """All completed attempts, including current, in acceptance order."""
        rows = self.connection.execute("SELECT result_json FROM attempts WHERE document_id=? AND result_json IS NOT NULL ORDER BY sequence",
                                       (document_id,)).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def snapshot(self):
        """Current terminal extractions and queue from one committed read view."""
        with self._transaction():
            rows = self.connection.execute("""SELECT a.result_json FROM documents d JOIN attempts a
                ON a.attempt_id=d.current_attempt_id ORDER BY d.document_id""").fetchall()
            extractions = [json.loads(row["result_json"]) for row in rows]
            fields = ("document_id", "source_sha256", "attempt_id", "revision", "policy_version",
                      "status", "owner", "next_action", "error", "response_source")
            queue = [{key: row[key] for key in fields} for row in extractions]
            for queued, record in zip(queue, extractions):
                if "retry_command" in record:
                    queued["retry_command"] = record["retry_command"]
        return {"extractions": extractions, "queue": queue}

    def progress(self):
        """Persisted work states; unfinished attempts never masquerade as results."""
        rows = self.connection.execute("SELECT * FROM attempts ORDER BY sequence").fetchall()
        return [self._pending(row) for row in rows]
