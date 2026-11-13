"""Read independent exports, keeping source identity separate from metadata."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .providers import Provider


class InputError(ValueError):
    """An export or batch cannot be interpreted unambiguously."""


@dataclass(frozen=True)
class SourceDocument:
    document_id: str
    source_sha256: str
    source_bytes: bytes
    message: str


@dataclass(frozen=True)
class InputResult:
    document_id: str
    source_sha256: str
    message: str | None
    response: dict[str, Any] | None
    error: dict[str, str] | None


def decode_export(path: Path, raw: bytes) -> SourceDocument:
    """Hash original bytes and expose only the decoded message to extraction."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError(
            f"{path.name}: invalid UTF-8 at byte {error.start}; re-export as UTF-8"
        ) from error
    try:
        export = json.loads(text)
    except json.JSONDecodeError as error:
        raise InputError(
            f"{path.name}: invalid JSON at line {error.lineno}, column {error.colno}; "
            "supply a valid JSON export"
        ) from error
    if not isinstance(export, dict):
        raise InputError(f"{path.name}: export must be a JSON object containing a message string")
    if not isinstance(export.get("message"), str):
        raise InputError(f"{path.name}: message is missing or is not a string; supply message text")
    return SourceDocument(
        document_id=path.name,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source_bytes=raw,
        message=export["message"],
    )


def batch_paths(paths: Iterable[Path]) -> list[Path]:
    """Check the whole batch for ambiguous filename identities before work."""
    batch = [Path(path) for path in paths]
    seen: set[str] = set()
    for path in batch:
        if path.name in seen:
            raise InputError(f"Duplicate basename {path.name}; use distinct filenames in a batch")
        seen.add(path.name)
    return batch


def process_exports(paths: Iterable[Path], *, provider: Provider) -> list[InputResult]:
    """Reject duplicate basenames before calls; isolate malformed input errors."""
    batch = batch_paths(paths)

    results = []
    for path in batch:
        raw = path.read_bytes()
        try:
            document = decode_export(path, raw)
        except InputError as error:
            results.append(InputResult(
                document_id=path.name,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                message=None,
                response=None,
                error={"code": "input-error", "message": str(error)},
            ))
            continue
        results.append(InputResult(
            document_id=document.document_id,
            source_sha256=document.source_sha256,
            message=document.message,
            response=provider.extract(document.message),
            error=None,
        ))
    return results
