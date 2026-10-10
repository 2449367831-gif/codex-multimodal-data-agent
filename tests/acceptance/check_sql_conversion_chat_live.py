"""Verify non-executing conversion through the running chat service, without rows."""

import http.cookiejar
import json
from pathlib import Path
import time
import urllib.request
from uuid import uuid4


def main():
    base = "http://127.0.0.1:8021"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Origin": base, **(headers or {})})
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    def wait(task):
        deadline = time.monotonic() + 45
        while task["state"] in {"queued", "running"}:
            if time.monotonic() > deadline:
                raise TimeoutError("conversion observation window expired")
            time.sleep(0.25)
            task = request("/v1/tasks/" + task["task_id"])
        return task

    session = request("/v1/session")
    if session.get("clarification_pending"):
        raise ValueError("existing clarification must remain untouched")
    cases = json.loads(Path(__file__).with_name("complex-query-cases.json").read_text(encoding="utf-8"))["cases"]
    question = next(case["question"] for case in cases if case["id"] == "T34")
    materials = '{"sql":"SELECT :quantity AS quantity, :note AS note","bindings":{"quantity":0.1234567890123456789,"note":"SYNTHETIC_PRIVATE_CONVERSION"},"table_mapping":{}}'
    checks = {}
    for state, label, raw in (
        ("needs_input", "待补充转换材料，未查询数据", None),
        ("converted", "转换检查完成，未查询数据", materials),
        ("rejected", "转换未通过，未查询数据", "{}"),
    ):
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "conversion-" + uuid4().hex}
        body = {"conversation_id": session["conversation_id"], "question": question}
        if raw is not None:
            body["sql_conversion"] = raw
        task = wait(request("/v1/tasks", body, headers))
        assert task["http_status"] == 200
        result = task["result"]
        checks[state + "_state"] = result["guidance"]["state"] == state and result["outcome"]["label"] == label
        checks[state + "_not_queried"] = result["result_state"] == "explanation" and result["process"] == {"state": "not_queried", "steps": []}
        checks[state + "_no_repeat_questions"] = result["clarification"] == {"required": False, "questions": []}
        checks[state + "_private"] = not any(key in result for key in ("sql", "business_result", "business_table", "business_tables", "mcp_calls")) and "SYNTHETIC_PRIVATE_CONVERSION" not in json.dumps(result)
        checks[state + "_owned"] = result["conversation_id"] == session["conversation_id"]
        checks[state + "_idempotent"] = request("/v1/tasks", body, headers)["task_id"] == task["task_id"]
    checks["canonical_conversation"] = request("/v1/session")["conversation_id"] == session["conversation_id"]
    print(json.dumps({"case": "T34", "checks": checks}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}), flush=True)
        raise SystemExit(2)
