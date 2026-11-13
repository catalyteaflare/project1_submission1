"""Exact comparisons against an independently authored source ledger.

Missing records and failed extractions remain in the denominator. This scorer
does not infer semantic equivalence or establish the quality of a live model.
"""

from .policy import POLICY_VERSION


FIELDS = ("request_id", "request_date", "kind", "requester", "team", "system",
          "environment", "permission", "business_purpose", "start_date", "end_date", "manager")


def compare(records, ledger):
    expected = {}
    for case in ledger.values():
        identity = case["document_id"]
        if identity in expected or set(case["scalars"]) != set(FIELDS):
            raise ValueError("Expected ledger needs unique documents and all 12 scalar fields")
        expected[identity] = case
    if not expected:
        raise ValueError("Expected ledger must not be empty")
    observed = {}
    for record in records:
        identity = record["document_id"]
        if identity in observed or identity not in expected:
            raise ValueError("Measured documents must be unique and present in the source ledger")
        if record.get("policy_version", POLICY_VERSION) != POLICY_VERSION:
            raise ValueError("Office-policy accuracy excludes test-only or other policy versions")
        observed[identity] = record

    report = {
        "scalar_matches": 0, "scalar_total": 12 * len(expected),
        "route_matches": 0, "route_total": len(expected),
        "per_field": {field: {"matches": 0, "total": len(expected)} for field in FIELDS},
        "per_document": {}, "factual_errors": [], "routing_errors": [],
        "evidence_errors": [], "issue_errors": [], "observations": [],
        "limits": "Literal JSON-decoded comparisons; purpose paraphrases require separate human factual assessment. Mock agreement is not live accuracy.",
    }
    for identity, case in expected.items():
        row = observed.get(identity)
        valid = row is not None and row.get("error") is None and isinstance(row.get("extraction"), dict)
        facts = row["extraction"] if valid else {}
        count = 0
        for field in FIELDS:
            value = case["scalars"][field]
            # Presence is distinct from a present JSON null, even for null oracles.
            if valid and field in facts and type(facts[field]) is type(value) and facts[field] == value:
                count += 1
                report["per_field"][field]["matches"] += 1
            else:
                report["factual_errors"].append({
                    "document_id": identity, "field": field, "expected": value,
                    "observed": facts.get(field), "source_quotes": list(case["source_quotes"]),
                    "reason": "missing-record" if row is None else "failed-extraction" if not valid else
                              "missing-field" if field not in facts else "exact-match-miss",
                })
        report["scalar_matches"] += count
        report["per_document"][identity] = {"matches": count, "total": 12}
        route = [row.get(key) for key in ("status", "owner", "next_action")] if row else None
        if valid and route == case["route"]:
            report["route_matches"] += 1
        else:
            report["routing_errors"].append({"document_id": identity, "expected": case["route"], "observed": route})

        quotes = facts.get("supporting_quotes", [])
        if not isinstance(quotes, list):
            quotes = []
        good_quotes = [quote for quote in quotes if isinstance(quote, str) and quote and quote in case["message"]]
        missing = [quote for quote in case["required_quotes"] if not any(quote in span for span in good_quotes)]
        if not valid or len(good_quotes) != len(quotes) or missing:
            report["evidence_errors"].append({"document_id": identity, "missing_required_quotes": missing,
                                               "invalid_quotes": [quote for quote in quotes if quote not in good_quotes]})
        issues = facts.get("issues", [])
        text = " ".join(issues).lower().replace("_", " ") if isinstance(issues, list) and all(isinstance(x, str) for x in issues) else ""
        terms = case["expected_issue_fields"] + case.get("issue_terms", [])
        missing_terms = [term for term in terms if term.lower().replace("_", " ") not in text]
        if not valid or missing_terms or (not terms and issues != []):
            report["issue_errors"].append({"document_id": identity, "missing_issue_terms": missing_terms,
                                            "observed": issues})
        report["observations"].append({
            "document_id": identity, "response_source": row.get("response_source") if row else "missing",
            "configuration": row.get("extraction_settings", row.get("settings")) if row else None,
            "source_sha256": row.get("source_sha256") if row else None,
            "attempt_id": row.get("attempt_id") if row else None,
        })
    return report
