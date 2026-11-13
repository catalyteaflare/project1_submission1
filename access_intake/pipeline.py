"""Compose export loading, response validation and human-review routing."""

from pathlib import Path
from typing import Any, Iterable

from .inputs import process_exports
from .policy import route
from .providers import Provider
from .validation import error_record, evaluate_response


def record_response(document_id: str, source_sha256: str, message: str, envelope: Any) -> dict[str, Any]:
    result = evaluate_response(envelope, message)
    if result["error"] is None:
        result.update(route(result["extraction"], message, document_id))
    return {"document_id": document_id, "source_sha256": source_sha256, **result}


def process_documents(paths: Iterable[Path], *, provider: Provider) -> list[dict[str, Any]]:
    results = []
    for loaded in process_exports(paths, provider=provider):
        if loaded.error:
            result = {
                "document_id": loaded.document_id,
                "source_sha256": loaded.source_sha256,
                **error_record(loaded.error["code"], loaded.error["message"], "not-called"),
            }
        else:
            result = record_response(loaded.document_id, loaded.source_sha256, loaded.message, loaded.response)
        results.append(result)
    return results
