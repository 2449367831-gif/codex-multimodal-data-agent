"""T30 independent public API verification; no approval changes or row files."""

from datetime import datetime
from decimal import Decimal
import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.outbound_link_planning import active_link_contract
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.knowledge_activation import active_snapshot
from app.query_outcomes import validate_doris_result
from check_outbound_links_live import expected_links, reference_queries


# Deliberately not imported from the production presenter: each requirement field
# must be checked independently, including the two different source-order levels.
FIELDS = {
    "cargo_owner":"cargo_owner", "outbound_code":"outbound_code",
    "referenced_order_code":"referenced_order_code", "linked_sales_order_code":"sales_order_code",
    "source_order_code":"source_order_code", "detail_source_order_code":"detail_source_order_code",
    "source_platform_code":"source_platform_code", "referenced_order_type":"ref_order_type",
    "order_type":"order_type", "link_ship_at":"ship_at", "relationship_state":"relationship_state",
}
STATES = {"已匹配销售订单":"matched", "未匹配销售订单":"unmatched"}


def acceptance_question():
    cases = json.loads((ROOT / "tests/acceptance/complex-query-cases.json").read_text(encoding="utf-8"))["cases"]
    return next(case["question"] for case in cases if case["id"] == "T30")


def instant(value):
    if value is None:
        return None
    result = datetime.fromisoformat(str(value))
    zone = ZoneInfo("Asia/Shanghai")
    return result.replace(tzinfo=zone) if result.tzinfo is None else result.astimezone(zone)


def signature(row):
    return tuple(instant(row[field]) if field == "ship_at" else row[field] for field in FIELDS.values())


def compare_table(table, reference):
    checks = {"public_columns":False, "public_row_fields":False, "unique_complete_links":False,
              "document_count":False, "complete_result":False, "all_owner_all_time_scope":False,
              "provenance_notes":False, **{f"field_{field}":False for field in FIELDS}}
    try:
        columns, rows = table["columns"], table["rows"]
        checks["public_columns"] = len(columns) == len(FIELDS) and [c["key"] for c in columns] == list(FIELDS) and all(c["kind"] == "text" for c in columns)
        checks["public_row_fields"] = all(set(row) == set(FIELDS) and all(value is None or isinstance(value, str) for value in row.values()) for row in rows)
        if not checks["public_row_fields"]:
            return checks
        actual = [{private:STATES[row[public]] if public == "relationship_state" else row[public]
                   for public, private in FIELDS.items()} for row in rows]
        key = lambda r:(r["cargo_owner"], r["outbound_code"], r["detail_source_order_code"])
        observed, expected = {key(r):r for r in actual}, {key(r):r for r in reference}
        checks["unique_complete_links"] = len(observed) == len(actual) == len(expected) == len(reference) and observed.keys() == expected.keys()
        for public, private in FIELDS.items():
            def value(row):
                return instant(row[private]) if private == "ship_at" else row[private]
            checks[f"field_{public}"] = checks["unique_complete_links"] and all(value(row) == value(expected[k]) for k, row in observed.items())
        summary = table["summary"]
        count = summary[0]["value"] if len(summary) == 1 else None
        number = Decimal(str(count)) if type(count) in (str, int) else Decimal("NaN")
        checks["document_count"] = len(summary) == 1 and summary[0]["label"] == "出库单数" and summary[0]["unit"] == "单" and number.is_finite() and number == len({(r["cargo_owner"],r["outbound_code"]) for r in reference})
        checks["complete_result"] = table.get("truncated", False) is False and not table.get("pagination")
        caption = table["caption"]
        checks["all_owner_all_time_scope"] = all(term in caption for term in ("全部时间", "全部货主", "最新出库的一张单"))
        notes = table["notes"]
        checks["provenance_notes"] = isinstance(notes, list) and len(notes) == 3 and all(isinstance(note, str) and note.strip() for note in notes)
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
        pass
    return checks


def latest_reference_sql():
    return """SELECT TRIM(h.company_code) AS cargo_owner, TRIM(h.outbound_code) AS outbound_code,
      MAX(h.ship_at) AS ship_at FROM ywzt_ods.scm_sales_shipment_header h
      WHERE COALESCE(h.delete_flag,0) <> 1
        AND COALESCE(LOWER(TRIM(h._fdl_marked_deleted)),'false') <> 'true'
        AND h.ship_at IS NOT NULL
      GROUP BY TRIM(h.company_code),TRIM(h.outbound_code)
      ORDER BY ship_at DESC,cargo_owner ASC,outbound_code ASC LIMIT 1"""


def latest_identity(rows):
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("invalid_reference_latest")
    row = rows[0]
    if any(not isinstance(row[k], str) or not row[k] or row[k] != row[k].strip() for k in ("cargo_owner", "outbound_code")):
        raise ValueError("invalid_reference_latest_identity")
    return row["cargo_owner"], row["outbound_code"], instant(row["ship_at"])


def read_reference(query):
    identity = latest_identity(query(latest_reference_sql()))
    if identity is None:
        return None, []
    sources = [query(sql) for sql in reference_queries((identity[1],))]
    after = latest_identity(query(latest_reference_sql()))
    if after != identity:
        raise ValueError("reference_latest_changed")
    # Only the selected owner/document participates. Unrelated owners sharing its
    # code are not allowed to influence either attributes or validation failures.
    scoped = [[row for row in rows if str(row.get("company_code") or "").strip() == identity[0]] for rows in sources]
    rows = expected_links(*scoped)
    if not rows or any((r["cargo_owner"], r["outbound_code"], instant(r["ship_at"])) != identity for r in rows):
        raise ValueError("reference_document_changed")
    return identity, rows


def verify(*, published, request, query, question, pause=time.sleep, report=lambda value:None):
    if not published:
        return {"case":"T30_chat", "state":"not_verified", "reason":"candidate_not_published"}
    session = request("/v1/session")
    if session.get("clarification_pending"):
        return {"case":"T30_chat", "state":"not_verified", "reason":"existing_user_clarification"}
    conversation = session["conversation_id"]

    def task(body):
        nonlocal conversation
        headers = {"X-CSRF-Token":session["csrf_token"], "Idempotency-Key":"t30-acceptance-" + uuid4().hex}
        item = request("/v1/tasks", body, headers)
        task_id = item["task_id"]
        last_report = 0
        while item["state"] in {"queued", "running"}:
            if time.monotonic() - last_report >= 30:
                report({"stage":"waiting_existing_task", "task_id":task_id, "state":item["state"]})
                last_report = time.monotonic()
            pause(1)
            try:
                item = request("/v1/tasks/" + task_id)
            except urllib.error.HTTPError as exc:
                if exc.code in {408, 429, 500, 502, 503, 504}:
                    continue
                raise
            except (TimeoutError, urllib.error.URLError):
                # An observation failure does not authorize another POST/model turn.
                continue
            if item.get("task_id") != task_id:
                raise ValueError("task_identity_changed")
        if item["state"] != "completed" or item.get("http_status") != 200:
            raise ValueError("task_not_completed")
        result = item["result"]
        if conversation and result.get("conversation_id") != conversation:
            raise ValueError("conversation_changed")
        conversation = result["conversation_id"]
        repeated = request("/v1/tasks", body, headers)
        if repeated.get("task_id") != task_id or repeated.get("result") != result:
            raise ValueError("idempotent_result_changed")
        return result

    first = task({"conversation_id":conversation, "question":question})
    card = first.get("clarification", {})
    questions = card.get("questions", [])
    if (not card.get("required") or not card.get("token") or len(questions) != 1 or
            questions[0].get("id") != "outbound_link_scope" or questions[0].get("selection") != "single" or
            [o.get("id") for o in questions[0].get("options", [])] != ["outbound_link_latest", "outbound_link_all"] or
            any(key in first for key in ("business_table", "business_tables"))):
        raise ValueError("invalid_original_clarification")
    before_identity, before = read_reference(query)
    selected = task({"conversation_id":conversation, "clarification_token":card["token"],
                     "clarification_answers":[{"question_id":"outbound_link_scope", "option_ids":["outbound_link_latest"]}]})
    after_identity, after = read_reference(query)
    if before_identity is None or after_identity is None:
        return {"case":"T30_chat", "state":"not_verified", "reason":"no_reference_document"}
    checks = compare_table(selected.get("business_table", {}), before)
    checks.update(independent_reference_stable=before_identity == after_identity and {signature(r) for r in before} == {signature(r) for r in after},
                  live_result=selected.get("result_state") == "live_success",
                  no_repeated_clarification=selected.get("clarification", {}).get("required") is False,
                  original_question=True, same_task_replay=True,
                  same_conversation=request("/v1/session").get("conversation_id") == conversation)
    return {"case":"T30_chat", "state":"verified" if all(checks.values()) else "failed", "checks":checks}


def main():
    _hide_windows_subprocesses()
    snapshot, digest = active_snapshot(ROOT)
    published = digest is not None and active_link_contract(snapshot)
    base = "http://127.0.0.1:8021"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type":"application/json", "Origin":base, **(headers or {})})
        with opener.open(req, timeout=30) as response:
            return json.load(response)
    def output(value):
        print(json.dumps(value), flush=True)
    if not published:
        output(verify(published=False, request=request, query=None, question=acceptance_question()))
        return 2
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    with bridge.request_session() as client:
        def query(sql):
            rows = validate_doris_result(client.query(sql=sql, purpose="T30 independent public relationship reconciliation")).rows
            if len(rows) >= 10000:
                raise ValueError("reference_capacity_exceeded")
            return rows
        result = verify(published=True, request=request, query=query, question=acceptance_question(), report=output)
    output(result)
    return 0 if result["state"] == "verified" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case":"T30_chat", "state":"failed", "error_type":type(exc).__name__}), flush=True)
        raise SystemExit(1)
