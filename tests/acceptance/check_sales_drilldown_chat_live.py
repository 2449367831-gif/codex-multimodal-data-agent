"""Reconcile every published T38 page without persisting business rows."""

from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import http.cookiejar
from importlib.util import module_from_spec, spec_from_file_location
import json
import re
from pathlib import Path
import sys
import time
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.sales_drilldown import SalesDrilldownPlan
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

RANK_KEYS = {"rank", "sku_code", "sku_name", "quantity", "outbound_count"}
DETAIL_KEYS = {"sku_code", "cargo_owner", "warehouse_code", "warehouse_name", "customer_code", "outbound_code", "ship_at", "quantity"}
PROGRESS = {"phase": "not_started", "completed_skus": 0, "completed_queries": 0}


def acceptance_question():
    cases = json.loads((ROOT / "tests/acceptance/complex-query-cases.json").read_text(encoding="utf-8"))["cases"]
    return next(case["question"] for case in cases if case["id"] == "T38")


def quantity_total(rows):
    return sum((Fraction(Decimal(str(row['quantity']))) for row in rows), Fraction())


def instant(value):
    parsed = datetime.fromisoformat(str(value))
    zone = ZoneInfo("Asia/Shanghai")
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def compare_public_ranking(rows, reference):
    checks = {"public_rank_fields": all(set(row) == RANK_KEYS for row in rows), "ranking_values_and_names": False}
    try:
        actual = [(int(r["rank"]), r["sku_code"], r["sku_name"], Decimal(str(r["quantity"])), int(r["outbound_count"])) for r in rows]
        expected = [(int(r["rank"]), r["sku_code"], r["sku_name"], Decimal(str(r["sku_quantity"])), int(r["outbound_count"])) for r in reference]
        checks["ranking_values_and_names"] = actual == expected
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    return checks


def compare_public_detail(ranked, rows, reference):
    checks = {"public_detail_fields": all(set(row) == DETAIL_KEYS for row in rows),
              "complete_unique_keys": False, "every_detail_value_and_name": False,
              "quantity_matches_original_rank": False, "owner_document_count_matches_original_rank": False}
    try:
        actual = {(r["sku_code"], r["cargo_owner"], r["outbound_code"]): (r["warehouse_code"], r["warehouse_name"], r["customer_code"], instant(r["ship_at"]), Decimal(str(r["quantity"]))) for r in rows}
        expected = {(r["sku_code"], r["owner"], r["outbound_code"]): (r["warehouse_code"], r["warehouse_name"], r["customer_code"], instant(r["ship_at"]), Decimal(str(r["quantity"]))) for r in reference}
        checks["complete_unique_keys"] = len(actual) == len(rows) == len(expected) == len(reference) and actual.keys() == expected.keys()
        checks["every_detail_value_and_name"] = actual == expected and all(r["sku_code"] == ranked["sku_code"] and r["sku_name"] == ranked["sku_name"] for r in reference)
        checks["quantity_matches_original_rank"] = quantity_total(rows) == Decimal(str(ranked["quantity"]))
        checks["owner_document_count_matches_original_rank"] = len({(r["cargo_owner"], r["outbound_code"]) for r in rows}) == int(ranked["outbound_count"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    return checks


def summary_matches(table, quantity, documents, *, ranking=False):
    labels = ("入榜实发数量", "入榜出库单数") if ranking else ("实发数量", "出库单数")
    try:
        items = table["summary"]
        return len(items) == 2 and {item["label"] for item in items} == set(labels) and all(
            Decimal(str(item["value"])) == (quantity if item["label"] == labels[0] else documents)
            and item["unit"] == ("EA" if item["label"] == labels[0] else "单") for item in items
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def complete_public_rows(table, keys, request):
    if {column["key"] for column in table["columns"]} != keys:
        raise ValueError("unexpected public columns")
    paging = table.get("pagination")
    rows = list(table["rows"])
    if not paging:
        return rows
    if (any(type(paging.get(key)) is not int or paging[key] < 1 for key in ("page", "page_count", "total_rows", "start_row", "end_row", "expires_at"))
            or not re.fullmatch(r"rp_[A-Za-z0-9_-]{32}", paging.get("result_id", ""))
            or paging["total_rows"] > 100000 or paging["page_count"] > paging["total_rows"]):
        raise ValueError("invalid page capacity")
    if paging["page"] != 1 or paging["start_row"] != 1 or paging["end_row"] != len(rows) or not paging["reconciled"]:
        raise ValueError("invalid first page")
    for number in range(2, paging["page_count"] + 1):
        page = request(f"/v1/results/{paging['result_id']}/pages/{number}")["business_table"]
        meta = page["pagination"]
        if (page["columns"] != table["columns"] or page["summary"] != table["summary"]
                or meta["page"] != number or meta["start_row"] != len(rows) + 1
                or meta["end_row"] != len(rows) + len(page["rows"])
                or any(meta[key] != paging[key] for key in ("result_id", "page_count", "total_rows", "expires_at", "reconciled"))):
            raise ValueError("invalid public page")
        rows.extend(page["rows"])
    if len(rows) != paging["total_rows"] or len(rows) > 100000:
        raise ValueError("incomplete public detail")
    return rows


def main():
    _hide_windows_subprocesses()
    base = "http://127.0.0.1:8021"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Origin": base, **(headers or {})})
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    session = request("/v1/session")
    if session.get("clarification_pending"):
        raise ValueError("existing user clarification must remain untouched")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    plan = SalesDrilldownPlan(today - timedelta(days=1), today)
    spec = spec_from_file_location("t38_independent_reference", Path(__file__).with_name("check_sales_drilldown_live.py"))
    reference_module = module_from_spec(spec)
    spec.loader.exec_module(reference_module)

    def task_result(body):
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "t38-acceptance-" + uuid4().hex}
        task = request("/v1/tasks", body, headers)
        last_report = 0
        while task["state"] in {"queued", "running"}:
            if time.monotonic() - last_report >= 30:
                print(json.dumps({"stage": "waiting_existing_task", "task_id": task["task_id"], "state": task["state"], "progress": PROGRESS}), flush=True)
                last_report = time.monotonic()
            time.sleep(1)
            task = request("/v1/tasks/" + task["task_id"])
        payload = task.get("result", {})
        if (task["state"] != "completed" or task.get("http_status") != 200 or payload.get("result_state") != "live_success"
                or payload.get("conversation_id") != session["conversation_id"] or payload.get("clarification", {}).get("required")):
            raise ValueError("task did not produce the required live result")
        repeated = request("/v1/tasks", body, headers)
        if repeated["task_id"] != task["task_id"] or repeated.get("result") != payload:
            raise ValueError("idempotent receipt changed")
        return payload

    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    paging = {"page_size": 2000, "page_cap": 2000}
    with bridge.request_session() as client:
        def read(sql, *, purpose):
            rows = validate_doris_result(client.query(sql=sql, purpose=purpose)).rows
            PROGRESS["completed_queries"] += 1
            return rows

        def many(statements, *, purpose, timeout_seconds):
            responses = client.query_many(statements=statements, purpose=purpose, timeout_seconds=timeout_seconds)
            PROGRESS["completed_queries"] += len(responses)
            return [validate_doris_result(response).rows for response in responses]

        PROGRESS["phase"] = "public_ranking"
        result = task_result({"conversation_id": session["conversation_id"], "question": acceptance_question()})
        table = result["business_table"]
        ranked = complete_public_rows(table, RANK_KEYS, request)
        context = table["drilldown"]
        PROGRESS["phase"] = "independent_ranking"
        reference = retrieve_sales_groups(reference_module.reference_ranking_sql(plan), read, query_many=many, **paging)
        checks = compare_public_ranking(ranked, reference)
        if not all(checks.values()):
            print(json.dumps({"case": "T38_chat", "checks": checks, "progress": PROGRESS}), flush=True)
            return 1
        selected_documents = set()
        for sku in ranked:
            PROGRESS["phase"] = "public_selected_detail"
            selection = {"result_id": context["result_id"], "sku_code": sku["sku_code"]}
            result = task_result({"conversation_id": session["conversation_id"], "drilldown": selection})
            if result.get("drilldown_source") != selection:
                raise ValueError("selected ranking context changed")
            rows = complete_public_rows(result["business_table"], DETAIL_KEYS, request)
            PROGRESS["phase"] = "independent_selected_detail"
            reference = retrieve_sales_groups(reference_module.reference_sql(plan, sku["sku_code"]), read, query_many=many, **paging)
            current = compare_public_detail(sku, rows, reference)
            current["public_detail_summary"] = summary_matches(result["business_table"], Decimal(str(sku["quantity"])), int(sku["outbound_count"]))
            if not all(current.values()):
                print(json.dumps({"case": "T38_chat", "checks": current, "progress": PROGRESS}), flush=True)
                return 1
            selected_documents.update((row["owner"], row["outbound_code"]) for row in reference)
            PROGRESS["completed_skus"] += 1
            print(json.dumps({"stage": "public_detail_reconciled", "checks": current, "progress": PROGRESS}), flush=True)
        checks.update(all_ranked_skus=PROGRESS["completed_skus"] == len(ranked), all_public_pages=True, same_receipt=True,
                      public_ranking_summary=summary_matches(table, quantity_total(ranked), len(selected_documents), ranking=True))
    PROGRESS["phase"] = "complete"
    print(json.dumps({"case": "T38_chat", "checks": checks, "progress": PROGRESS}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case": "T38_chat", "passed": False, "error_type": type(exc).__name__, "reason": getattr(exc, "reason", None), "progress": PROGRESS}), flush=True)
        raise SystemExit(1)
