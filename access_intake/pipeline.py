"""Compose export loading, response validation and human-review routing."""

import hashlib
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .inputs import InputError, SourceDocument, batch_paths, decode_export
from .policy import route
from .providers import Provider, ProviderUnavailable
from .validation import error_record, evaluate_response


def record_response(document_id: str, source_sha256: str, message: str, envelope: Any) -> dict[str, Any]:
    result = evaluate_response(envelope, message)
    if result["error"] is None:
        result.update(route(result["extraction"], message, document_id))
    return {"document_id": document_id, "source_sha256": source_sha256, **result}


def process_source(document: SourceDocument, provider: Provider) -> dict[str, Any]:
    try:
        envelope = provider.extract(document.message)
    except (ProviderUnavailable, TimeoutError) as error:
        default_code = "provider-timeout" if isinstance(error, TimeoutError) else "provider-unavailable"
        # Original transport diagnostics may contain credentials or request data.
        result = error_record(
            getattr(error, "code", default_code),
            "Provider extraction failed; inspect configuration and retry the saved attempt",
            getattr(provider, "response_source", "unknown"),
        )
        return {"document_id": document.document_id, "source_sha256": document.source_sha256, **result}
    return record_response(document.document_id, document.source_sha256, document.message, envelope)


class IntakeService:
    """Isolated per-document calls and injected retries using saved source bytes.

    Sources here live in memory. Durable attempts belong to step 6; the retry
    command is a description for the later operator CLI, not an executable yet.
    """

    def __init__(self, *, provider: Provider, retry_handler: Callable = process_source):
        self.provider = provider
        self.retry_handler = retry_handler
        self._sources: dict[str, SourceDocument] = {}

    def _attempt(self, document: SourceDocument, provider: Provider, handler: Callable = process_source):
        attempt_id = uuid4().hex
        self._sources[attempt_id] = document
        result = handler(document, provider)
        result["attempt_id"] = attempt_id
        if result["error"]:
            result["retry_command"] = f"python3 -m access_intake retry --attempt {attempt_id} --state STATE"
        return result

    def process(self, paths: Iterable[Path]) -> list[dict[str, Any]]:
        results = []
        for path in batch_paths(paths):
            raw = path.read_bytes()
            try:
                document = decode_export(path, raw)
            except InputError as error:
                results.append({
                    "document_id": path.name, "source_sha256": hashlib.sha256(raw).hexdigest(),
                    **error_record("input-error", str(error), "not-called"),
                })
                continue
            results.append(self._attempt(document, self.provider))
        return results

    def retry(self, attempt_id: str, *, provider: Provider) -> dict[str, Any]:
        return self._attempt(self._sources[attempt_id], provider, self.retry_handler)


def process_documents(paths: Iterable[Path], *, provider: Provider) -> list[dict[str, Any]]:
    return IntakeService(provider=provider).process(paths)
