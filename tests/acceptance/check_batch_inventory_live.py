"""Reconcile the public batch answer against independent MCP aggregates in memory."""

from datetime import date, timedelta
from decimal import Decimal
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient


def reference_sql(production_date):
    start = production_date.isoformat()
    end = (production_date + timedelta(days=1)).isoformat()
    # EXISTS makes SKU qualification independent of the runtime's ranked join.
    # Omitted owner is ALL, exactly as in the standalone public question.
    return f"""SELECT COUNT(*) AS record_count,
        COUNT(i.in_stock_quantity) AS stock_count, SUM(i.in_stock_quantity) AS stock_sum,
        COUNT(i.actual_quantity) AS actual_count, SUM(i.actual_quantity) AS actual_sum,
        MAX(i.stock_change_time) AS freshness
    FROM ywzt_ods.scm_warehous_manage i
    WHERE TRIM(i.wms_code)='10114790'
      AND i.production_date>='{start} 00:00:00' AND i.production_date<'{end} 00:00:00'
      AND COALESCE(TRIM(CAST(i.status AS STRING)),'0')='0'
      AND COALESCE(TRIM(CAST(i.inventory_status AS STRING)),'') IN ('','ZP')
      AND EXISTS (SELECT 1 FROM ywzt_ods.master_sku s
        WHERE TRIM(s.sku_code)=TRIM(i.sku_code) AND TRIM(s.sku_group)='50'
          AND COALESCE(TRIM(CAST(s.status AS STRING)),'0')='0'
          AND (s.sku_name LIKE '%烤肠%' OR s.sku_name LIKE '%香肠%' OR s.sku_name LIKE '%脆皮肠%'))"""


def compare(result, reference):
    count = int(reference["record_count"])
    table = result.get("business_table") or {}
    rows = table.get("rows", [])
    text = (result.get("business_result") or {}).get("text", "")
    checks = {
        "completed_response": result.get("status") == "completed",
        "no_repeated_clarification": not (result.get("clarification") or {}).get("required", True),
        "table_present": bool(table.get("columns")),
        "scalar_grain": len(rows) == 1 if count else len(rows) <= 1,
        "current_result_state": result.get("result_state") == ("live_success" if count else "no_data"),
        "no_current_zero_claim_when_absent": bool(count) or (
            "不能据此确认库存为零" in text and "当前库存：0" not in text and "当前实际库存：0" not in text),
    }
    for field, prefix in (("current_stock", "stock"), ("available_stock", "actual")):
        complete = count > 0 and int(reference[prefix + "_count"]) == count
        expected = Decimal(str(reference[prefix + "_sum"])) if complete else None
        actual = rows[0].get(field) if rows else None
        checks[field + "_matches"] = (
            actual is None if expected is None else actual is not None and Decimal(str(actual)) == expected
        )
    wire = json.dumps(result, ensure_ascii=False).casefold()
    checks["no_internal_evidence"] = not any(
        marker in wire for marker in ("scm_warehous_manage", "stock_value_count", "select ", "password=", "traceback")
    )
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-date", type=date.fromisoformat, default=date(2026, 8, 3))
    args = parser.parse_args()
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    def reference():
        checked = validate_doris_result(bridge.query(
            sql=reference_sql(args.production_date), purpose="batch inventory independent completeness reference"))
        if len(checked.rows) != 1:
            raise ValueError("incomplete scalar reference")
        return checked.rows[0]

    before = reference()
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    question = f"查询{args.production_date.isoformat()}生产日期的烤肠在长沙黄兴的库存量，用表格展示。"
    task = client.request("/v1/tasks", {"question": question, "conversation_id": session["conversation_id"]},
                          session["csrf_token"], "batch-acceptance-" + uuid4().hex)
    deadline = time.monotonic() + 300
    while task["state"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(1)
        task = client.request("/v1/tasks/" + task["task_id"])
    if task["state"] in {"queued", "running"}:
        # An observation timeout is not a cancellation or a restart.
        print(json.dumps({"case": "batch_inventory", "task_id": task["task_id"], "state": task["state"]}))
        return 2
    after = reference()
    checks = compare(task.get("result") or {}, after)
    checks["stable_reference"] = before == after
    checks["task_completed"] = task["state"] == "completed"
    print(json.dumps({"case": "batch_inventory", "checks": checks}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case": "batch_inventory", "state": "verification_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(2) from None
