"""Reproduce step 5 request evidence using a saved reply and fake transport.

Run from the repository root:
    env -u API_KEY python3 -m tools.record_request_evidence
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from access_intake.http_provider import HttpProvider, HttpReply
from access_intake.pipeline import record_response


ROOT = Path(__file__).resolve().parents[1]


def main():
    request_path = ROOT / "materials" / "requests" / "IT-001.json"
    raw = request_path.read_bytes()
    message = json.loads(raw.decode("utf-8"))["message"]
    fixture_name = "tests/mock_responses/http-complete.json"
    reply = json.loads((ROOT / fixture_name).read_text(encoding="utf-8"))
    recorded = []

    def fake_transport(**request):
        recorded.append(request)
        return HttpReply(reply["status"], reply["headers"], json.dumps(reply["body"]).encode("utf-8"))

    provider = HttpProvider.from_environment(
        environment={
            "API_KEY": "dummy-evidence-only", "MODEL_PROVIDER": "test-provider", "MODEL_ID": "test-model",
            "MODEL_ENDPOINT": "https://example.invalid/v1/chat/completions",
            "MODEL_TIMEOUT_SECONDS": "12", "MODEL_MAX_RESPONSE_BYTES": "65536",
        },
        transport=fake_transport,
    )
    result = record_response(request_path.name, hashlib.sha256(raw).hexdigest(), message, provider.extract(message))
    evidence = provider.request_evidence(message)
    assert evidence["request"]["body"] == json.loads(recorded[0]["body"])
    assert evidence["request"]["endpoint"] == recorded[0]["endpoint"]
    evidence["observation"] = {
        "response_origin": "fake HTTP transport",
        "configuration": asdict(provider.config),
        "fixture": fixture_name,
        "source": "materials/requests/IT-001.json",
        "source_sha256": result["source_sha256"],
        "response_source": result["response_source"],
        "status": result["status"], "owner": result["owner"], "next_action": result["next_action"],
        "error": result["error"],
        "scope": "Actual adapter with saved HTTP reply; no live provider request or real credential used",
    }
    destination = ROOT / "deliverables" / "request-evidence.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Recorded redacted request evidence: test-provider/test-model; fake HTTP transport")


if __name__ == "__main__":
    main()
