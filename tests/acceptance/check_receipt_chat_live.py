"""Verify screened receipt pages end to end; never persist business rows."""

from collections import defaultdict
import argparse
from datetime import datetime, timedelta
from decimal import Decimal
import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from check_purchase_receipts_live import independent_controls


def main():
    _hide_windows_subprocesses()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", choices=("ALL", "HJXH"), default="ALL")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        parser.error("days must be between 1 and 3650")
    base = "http://127.0.0.1:8021"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Origin": base, **(headers or {})})
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    session = request("/v1/session")
    headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "receipt-acceptance-" + uuid4().hex}
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = today - timedelta(days=args.days)
    question = (f"查询近{args.days}个完整自然日生产日期的采购入库明细，但不包含今天完成入库的数据。"
                "返回仓库、入库单、SKU、生产日期、实收入库数量和实际入库时间。")
    if args.owner != "ALL":
        question = args.owner + "货主" + question
    body = {"question": question, "conversation_id": session["conversation_id"]}
    task = request("/v1/tasks", body, headers)
    print(json.dumps({"task_id": task["task_id"], "state": task["state"]}), flush=True)
    deadline = time.monotonic() + 300
    while task["state"] in {"queued", "running"}:
        if time.monotonic() >= deadline:
            print(json.dumps({"state": "observation_timeout", "task_id": task["task_id"]}), flush=True)
            return 2
        time.sleep(1)
        task = request("/v1/tasks/" + task["task_id"])
    result = task.get("result", {})
    if task.get("http_status") != 200 or result.get("result_state") != "live_success":
        print(json.dumps({"state": task["state"], "http_status": task.get("http_status"), "result_state": result.get("result_state"), "error_code": result.get("error", {}).get("code")}))
        return 1
    table = result["business_table"]
    paging = table.get("pagination")
    assert paging and paging["reconciled"] is True
    rows = []
    expected_columns = {"cargo_owner", "inbound_code", "detail_id", "item_code", "warehouse_code", "warehouse_name", "sku_code", "batch_number", "production_date", "received_quantity", "quantity_unit", "actual_arrival_date"}
    for number in range(1, paging["page_count"] + 1):
        page = request(f'/v1/results/{paging["result_id"]}/pages/{number}')["business_table"]
        meta = page["pagination"]
        assert {col["key"] for col in page["columns"]} == expected_columns
        assert meta["result_id"] == paging["result_id"] and meta["reconciled"] is True
        assert meta["total_rows"] == paging["total_rows"] and meta["page_count"] == paging["page_count"]
        assert meta["page"] == number and meta["start_row"] == len(rows) + 1
        assert 1 <= len(page["rows"]) <= 100
        rows.extend(page["rows"])
        assert meta["end_row"] == len(rows)
    totals = defaultdict(Decimal)
    keys = set()
    for row in rows:
        key = (row["cargo_owner"], row["inbound_code"], row["detail_id"])
        assert key not in keys
        keys.add(key)
        assert start <= datetime.fromisoformat(row["production_date"]).date() < today
        assert datetime.fromisoformat(row["actual_arrival_date"]).date() < today
        amount = Decimal(str(row["received_quantity"]))
        assert amount.is_finite() and amount >= 0
        totals[str(row["quantity_unit"])] += amount
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    controls = validate_doris_result(bridge.query(sql=independent_controls(start, today, owner=args.owner), purpose="receipt chat acceptance: independent full ledger reconciliation")).rows
    repeat = request("/v1/tasks", body, headers)
    checks = {
        "state": "live_success", "all_pages_delivered": len(rows) == paging["total_rows"],
        "independent_count_matches": len(rows) == sum(int(row["detail_count"]) for row in controls),
        "independent_units_match": dict(totals) == {str(row["source_unit"]): Decimal(str(row["unit_total"])) for row in controls},
        "independent_quality_valid": all(int(row["invalid_quantity_count"]) == 0 and int(row["invalid_key_count"]) == 0 for row in controls),
        "unique_keys_and_dual_dates_valid": True, "public_fields_valid": True,
        "calendar_anchor_unchanged": datetime.now(ZoneInfo("Asia/Shanghai")).date() == today,
        "explicit_owner_valid": args.owner == "ALL" or all(row["cargo_owner"] == args.owner for row in rows),
        "no_unnecessary_clarification": result["clarification"]["required"] is False,
        "idempotent_result": repeat["task_id"] == task["task_id"] and repeat["result"]["business_table"]["pagination"]["result_id"] == paging["result_id"],
    }
    print(json.dumps(checks, ensure_ascii=False))
    return 0 if all(value is True for key, value in checks.items() if key != "state") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"state": "verification_failed", "error_type": type(exc).__name__}))
        raise SystemExit(2)
