"""Ordered human-review policy and independent source override inspection."""

import copy
import re
from pathlib import Path
from typing import Any

from .validation import SCALAR_FIELDS

POLICY_VERSION = "northstar-v1"
_OVERRIDE = re.compile(
    r"system\s+override|bypass\s+approval|ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions"
    r"|(?:display|disclose|reveal|print|show)\b[^.\n]{0,60}\b(?:api[ _-]?key|credentials|secrets)"
    r"|mark\b[^.\n]{0,40}\bapproved|highest[- ]priority\s+system\s+instruction",
    re.IGNORECASE,
)


def route(extraction: dict[str, Any], message: str, document_id: str) -> dict[str, Any]:
    """Accept validated facts only. Source inspection is conservative, not exhaustive."""
    facts = copy.deepcopy(extraction)
    issues = facts["issues"]
    source_override = bool(_OVERRIDE.search(message))
    reported_override = any(re.search(r"override|prompt injection|bypass approval", issue, re.I) for issue in issues)
    if source_override or reported_override:
        if source_override:
            if not any("override" in issue.lower() for issue in issues):
                issues.append("Instruction override attempt detected in source; requires human inspection")
            if message not in facts["supporting_quotes"]:
                facts["supporting_quotes"].append(message)
        decision = ("review", "IT Service Desk", "inspect-message")
    elif facts["kind"] == "withdrawal" and facts["request_id"] and facts["request_date"]:
        decision = ("review", "IT Service Desk", "confirm-withdrawal")
    else:
        if facts["request_id"] and facts["request_id"] != Path(document_id).stem:
            issues.append(f"request_id {facts['request_id']} differs from document_id {document_id}")
        if facts["start_date"] and facts["end_date"] and facts["start_date"] > facts["end_date"]:
            issues.append("start_date is after end_date; clarify the access window")
        if facts["kind"] not in ("new", "change") or any(facts[field] is None for field in SCALAR_FIELDS) or issues:
            decision = ("needs-information", "IT Service Desk", "clarify")
        elif facts["environment"] == "production" or facts["permission"] in ("write", "admin"):
            decision = ("review", "Security Review", "review-risk-then-owner")
        else:
            decision = ("review", "System Owner", "review-request")
    return {"extraction": facts, **dict(zip(("status", "owner", "next_action"), decision))}
