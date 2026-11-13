"""Deterministic response gates. Failed data never reaches policy routing."""

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "snapshot.schema.json").read_text())
SCALAR_FIELDS = tuple(name for name in SCHEMA["required"] if name not in ("issues", "supporting_quotes"))


class ExtractionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def error_record(code: str, diagnostic: str, response_source: str = "unknown") -> dict[str, Any]:
    return {
        "extraction": None, "status": "error", "owner": "IT Service Desk",
        "next_action": "retry-extraction", "error": {"code": code, "message": diagnostic},
        "response_source": response_source,
    }


def _invalid(message: str) -> None:
    raise ExtractionError("invalid-extraction", message)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite JSON number")


def validate_response(envelope: Any, message: str) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise ExtractionError("invalid-envelope", "Provider response must be an envelope object")
    if envelope.get("refusal"):
        raise ExtractionError("refusal", "Provider refused extraction; inspect the message before retrying")
    status = envelope.get("status")
    if status == "incomplete":
        raise ExtractionError("incomplete", "Provider response was incomplete; retry extraction")
    if status != "completed" or not isinstance(envelope.get("output_text"), str):
        raise ExtractionError("invalid-envelope", "Expected completed status and string output_text")
    try:
        extraction = json.loads(envelope["output_text"], object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise ExtractionError("invalid-json", "Provider output is not valid JSON; retry extraction") from error
    if not isinstance(extraction, dict):
        _invalid("Extraction must be a JSON object")
    if set(extraction) != set(SCHEMA["required"]):
        _invalid("Extraction must contain exactly the 14 schema properties")
    for field, specification in SCHEMA["properties"].items():
        value = extraction[field]
        if field in ("supporting_quotes", "issues"):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                _invalid(f"{field} must be an array of strings")
        else:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                _invalid(f"{field} must be a nonempty string or null")
            if "enum" in specification and value not in specification["enum"]:
                _invalid(f"{field} is outside the schema enum")
            if value is not None and field in ("request_date", "start_date", "end_date"):
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError("Not ISO date")
                    date.fromisoformat(value)
                except ValueError:
                    _invalid(f"{field} must be a real YYYY-MM-DD calendar date")
    issue_text = " ".join(extraction["issues"]).lower().replace("_", " ")
    for field in SCALAR_FIELDS:
        if extraction[field] is None and field.replace("_", " ") not in issue_text:
            _invalid(f"Null {field} requires a field-specific issue")
    for quote in extraction["supporting_quotes"]:
        if not quote or quote not in message:
            _invalid("Each supporting quote must be a nonempty exact source substring")
    return extraction


def evaluate_response(envelope: Any, message: str) -> dict[str, Any]:
    """Return validated facts or a structured error; successful facts await routing."""
    source = envelope.get("response_source", "mock") if isinstance(envelope, dict) else "unknown"
    try:
        extraction = validate_response(envelope, message)
    except ExtractionError as error:
        return error_record(error.code, str(error), source)
    return {"extraction": extraction, "error": None, "response_source": source}
