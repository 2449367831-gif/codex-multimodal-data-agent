"""Exercise proposal ownership on the running service without querying business data."""

import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.knowledge_proposals import snapshot_digest
from app.proposal_review import active_proposal_snapshot


def main():
    base = "http://127.0.0.1:8021"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Origin": base, **(headers or {})})
        try:
            with opener.open(req, timeout=20) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    snapshot = active_proposal_snapshot(ROOT)
    original = snapshot_digest(snapshot)
    metric = snapshot.contracts["metric:sales.outbound_actual"]
    status, session = request("/v1/session")
    assert status == 200
    headers = {"X-CSRF-Token": session["csrf_token"]}
    binding = {"conversation_id": session["conversation_id"], "display_session_id": "verify-" + uuid4().hex,
               "request_id": "verify-" + uuid4().hex}
    proposal = {"baseline_sha256": original, "changes": [{"kind": "metric", "id": metric["id"],
                "set": {"display_name": "仅本次验收的临时指标名称"}, "remove": []}]}
    body = {**binding, "proposal": json.dumps(proposal)}
    checks = {}
    opened_id = None
    try:
        status, opened = request("/v1/knowledge-proposals", body, headers)
        checks["created"] = status == 200
        if not checks["created"]:
            raise RuntimeError("proposal creation failed")
        opened_id = opened["proposal_id"]
        url = "/v1/knowledge-proposals/" + opened_id
        checks["not_queried"] = opened["result_state"] == "definition_review" and opened["process"] == {"state": "not_queried", "steps": []}
        checks["comparison"] = opened["assessment"]["comparisons"][0]["before"] == metric["display_name"] and opened["assessment"]["comparisons"][0]["after"] == "仅本次验收的临时指标名称"
        checks["screened"] = not any(value in json.dumps(opened) for value in ["baseline_sha256", "source_tables", "business_table", "business_result", metric["id"]])
        checks["create_replay"] = request("/v1/knowledge-proposals", body, headers)[1] == opened
        checks["resume"] = request(url + "/resume", binding, headers)[1] == opened
        checks["wrong_display_session"] = request(url + "/resume", {**binding, "display_session_id": "foreign-display-session"}, headers)[0] == 404
        checks["wrong_request"] = request(url + "/resume", {**binding, "request_id": "foreign-request123"}, headers)[0] == 404
        checks["csrf"] = request(url + "/cancel", binding)[0] == 403
        answers = {q["id"]: "defer" for q in opened["assessment"]["questions"]}
        status, deferred = request(url + "/select", {**binding, "revision": 0, "choices": answers}, headers)
        checks["defer"] = status == 200 and deferred["revision"] == 1 and deferred["assessment"]["requires_confirmation"]
        selected_body = {**binding, "revision": 1, "choices": {key: "current" for key in answers}}
        status, selected = request(url + "/select", selected_body, headers)
        checks["select_current"] = status == 200 and selected["revision"] == 2 and not selected["assessment"]["requires_confirmation"]
        checks["no_publication"] = selected["assessment"]["publication_authorized"] is False
        projection = subprocess.run(
            ["node", str(ROOT / "tests/acceptance/check_proposal_public_contract.cjs")],
            input=json.dumps([opened, deferred, selected]), text=True, capture_output=True,
            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        checks["frontend_contract"] = projection.returncode == 0
        checks["selection_replay"] = request(url + "/select", selected_body, headers)[1] == selected
        checks["conflicting_replay"] = request(url + "/select", {**selected_body, "choices": {key: "proposed" for key in answers}}, headers)[0] == 409
        checks["cancel"] = request(url + "/cancel", binding, headers) == (200, {"state": "cancelled"})
        checks["cancelled_resume"] = request(url + "/resume", binding, headers)[0] == 410
        close_body = {key: value for key, value in binding.items() if key != "request_id"}
        checks["close_session"] = request("/v1/knowledge-proposal-sessions/close", close_body, headers) == (200, {"state": "closed"})
        checks["late_create"] = request("/v1/knowledge-proposals", {**body, "request_id": "late-" + uuid4().hex}, headers)[0] == 410
        fresh = {**binding, "display_session_id": "verify-" + uuid4().hex, "request_id": "verify-" + uuid4().hex}
        stale = {**proposal, "baseline_sha256": "0" * 64}
        checks["stale_baseline"] = request("/v1/knowledge-proposals", {**fresh, "proposal": json.dumps(stale)}, headers)[0] == 409
        checks["invalid_material"] = request("/v1/knowledge-proposals", {**fresh, "proposal": "{}"}, headers)[0] == 422
        checks["canonical_conversation"] = request("/v1/session")[1]["conversation_id"] == binding["conversation_id"]
        checks["knowledge_unchanged"] = snapshot_digest(active_proposal_snapshot(ROOT)) == original
    finally:
        if opened_id is not None:
            request("/v1/knowledge-proposals/" + opened_id + "/cancel", binding, headers)
    print(json.dumps({"checks": checks, "database_queried": False}, ensure_ascii=True), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}), flush=True)
        raise SystemExit(2)
