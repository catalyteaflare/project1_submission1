"""Validate completed provider envelopes before exposing facts to routing."""

import json
import re
from datetime import date
from pathlib import Path
from typing import Any


SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "snapshot.schema.json").read_text(encoding="utf-8")
)
SCALAR_FIELDS = tuple(
    field for field in SCHEMA["required"] if field not in ("supporting_quotes", "issues")
)


class ExtractionError(ValueError):
    """A rejected result with a stable, distinguishable failure code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def error_record(code: str, diagnostic: str, response_source: str = "unknown") -> dict[str, Any]:
    return {
        "extraction": None,
        "status": "error",
        "owner": "IT Service Desk",
        "next_action": "retry-extraction",
        "error": {"code": code, "message": diagnostic},
        "response_source": response_source,
    }


def _invalid(diagnostic: str) -> None:
    raise ExtractionError("invalid-extraction", diagnostic)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite numbers are not JSON values")


def validate_response(envelope: Any, message: str) -> dict[str, Any]:
    """Return validated facts or raise ExtractionError; never repair output.

    Dates are checked individually. A reversed access window remains as stated
    for the later clarification policy. Quote membership cannot prove that all
    extracted facts are supported; source-derived accuracy checks remain needed.
    """
    if not isinstance(envelope, dict):
        raise ExtractionError("invalid-envelope", "Expected a provider envelope object; inspect response handling")
    refusal = envelope.get("refusal")
    if refusal is not None and not isinstance(refusal, str):
        raise ExtractionError("invalid-envelope", "Expected a string or null refusal indicator")
    if refusal:
        raise ExtractionError("refusal", "Provider refused extraction; inspect the message before retrying")
    status = envelope.get("status")
    if status == "incomplete":
        raise ExtractionError("incomplete", "Provider response was incomplete; retry extraction")
    if status != "completed" or not isinstance(envelope.get("output_text"), str):
        raise ExtractionError("invalid-envelope", "Expected completed status and string output_text; inspect response handling")
    try:
        facts = json.loads(
            envelope["output_text"], object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError) as error:
        raise ExtractionError("invalid-json", "Provider output is not valid JSON; retry extraction") from error

    if not isinstance(facts, dict):
        _invalid("Extraction must be a JSON object")
    if set(facts) != set(SCHEMA["required"]):
        _invalid("Extraction must contain exactly the 14 required schema properties")
    for field, specification in SCHEMA["properties"].items():
        value = facts[field]
        if specification["type"] == "array":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                _invalid(f"{field} must be an array containing only strings")
            continue
        if value is not None and (not isinstance(value, str) or not value.strip()):
            _invalid(f"{field} must be a nonempty string or null")
        if "enum" in specification and value not in specification["enum"]:
            _invalid(f"{field} must use an allowed schema value")
        if field in ("request_date", "start_date", "end_date") and value is not None:
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError("Invalid date representation")
                date.fromisoformat(value)
            except ValueError:
                _invalid(f"{field} must be a real calendar date written as YYYY-MM-DD")

    issue_text = " ".join(facts["issues"]).lower().replace("_", " ")
    for field in SCALAR_FIELDS:
        field_name = field.replace("_", " ")
        if facts[field] is None and not re.search(r"\b" + re.escape(field_name) + r"\b", issue_text):
            _invalid(f"Null {field} requires a field-specific missing or unresolved fact in issues")
    for quote in facts["supporting_quotes"]:
        if not quote or quote not in message:
            _invalid("Each supporting quote must be a nonempty exact substring of the decoded message")
    return facts


def evaluate_response(envelope: Any, message: str) -> dict[str, Any]:
    """Produce validated facts or an error record; successful facts await routing."""
    source = envelope.get("response_source") if isinstance(envelope, dict) else None
    source = source if isinstance(source, str) and source else "unknown"
    try:
        extraction = validate_response(envelope, message)
    except ExtractionError as error:
        return error_record(error.code, str(error), source)
    return {"extraction": extraction, "error": None, "response_source": source}
