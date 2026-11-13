"""Ordered human review for validated facts, with independent source inspection.

Override detection is conservative and not exhaustive. Quoted examples and
requests discussing these instructions can produce false positives; they go to
human inspection. Ordinary access requests do not trigger these patterns.
"""

import copy
import re
from pathlib import Path
from typing import Any

from .validation import SCALAR_FIELDS


POLICY_VERSION = "northstar-v1"
_SOURCE_OVERRIDE = re.compile(
    r"\bsystem\s+override\b"
    r"|\bbypass\s+approval\b"
    r"|\bignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions\b"
    r"|\b(?:display|disclose|reveal|print|show)\b[^.\n]{0,60}\b(?:api[ _-]?key|credentials|secrets)\b"
    r"|\bmark\b[^.\n]{0,40}\bapproved\b"
    r"|\bhighest[- ]priority\s+system\s+instruction\b",
    re.IGNORECASE,
)
_REPORTED_OVERRIDE = re.compile(r"\boverride\b|\bprompt injection\b|\bbypass approval\b", re.IGNORECASE)


def _add_issue(facts: dict[str, Any], issue: str) -> None:
    if issue not in facts["issues"]:
        facts["issues"].append(issue)


def route(extraction: dict[str, Any], message: str, document_id: str) -> dict[str, Any]:
    """Route already validated facts, retaining source evidence and uncertainty."""
    facts = copy.deepcopy(extraction)
    source_override = bool(_SOURCE_OVERRIDE.search(message))
    reported_override = any(_REPORTED_OVERRIDE.search(issue) for issue in facts["issues"])
    if source_override:
        if not reported_override:
            _add_issue(facts, "Instruction override attempt detected in source; requires human inspection")
        # Keep the full exact source so adjacent directives are not dropped.
        if message not in facts["supporting_quotes"]:
            facts["supporting_quotes"].append(message)
    if facts["request_id"] and facts["request_id"] != Path(document_id).stem:
        _add_issue(facts, f"request_id {facts['request_id']} differs from document_id {document_id}")
    if facts["start_date"] and facts["end_date"] and facts["start_date"] > facts["end_date"]:
        _add_issue(facts, "start_date is after end_date; clarify the access window")

    if source_override or reported_override:
        decision = ("review", "IT Service Desk", "inspect-message")
    elif facts["kind"] == "withdrawal" and facts["request_id"] and facts["request_date"]:
        decision = ("review", "IT Service Desk", "confirm-withdrawal")
    elif (
        facts["kind"] not in ("new", "change")
        or any(facts[field] is None for field in SCALAR_FIELDS)
        or facts["issues"]
    ):
        decision = ("needs-information", "IT Service Desk", "clarify")
    elif facts["environment"] == "production" or facts["permission"] in ("write", "admin"):
        decision = ("review", "Security Review", "review-risk-then-owner")
    else:
        decision = ("review", "System Owner", "review-request")
    return {"extraction": facts, **dict(zip(("status", "owner", "next_action"), decision))}
