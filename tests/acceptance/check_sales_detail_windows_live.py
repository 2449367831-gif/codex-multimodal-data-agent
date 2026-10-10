"""Live calendar-window detail acceptance; never write or print business rows."""

import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlglot import exp


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient


def independent_control_sql(start, end, *, warehouse=None):
    """Direct source ledger, independent of the production partition compiler."""
    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("invalid reference interval")
    if warehouse is not None and (not isinstance(warehouse, str) or not warehouse.strip()):
        raise ValueError("invalid reference warehouse")
    warehouse_filter = (" AND TRIM(h.facility_code)=" + exp.Literal.string(warehouse).sql(dialect="doris")) if warehouse else ""
    return f"""WITH eligible AS (
  SELECT DISTINCT TRIM(h.company_code) AS owner, TRIM(h.outbound_code) AS document,
    TRIM(h.facility_code) AS warehouse, h.ship_at, CAST(h.status AS STRING) AS shipment_status,
    TRIM(h.ref_order_type) AS order_type, TRIM(h.ref_order_code) AS order_code,
    CASE WHEN TRIM(CAST(h.terms_of_shipment AS STRING)) = '20' THEN '2C'
      WHEN NULLIF(TRIM(CAST(h.terms_of_shipment AS STRING)), '') IS NULL THEN 'missing'
      ELSE '2B' END AS fulfillment
  FROM ywzt_ods.scm_sales_shipment_header h
  WHERE h.ship_at >= '{start.isoformat()} 00:00:00' AND h.ship_at < '{end.isoformat()} 00:00:00'
    {warehouse_filter}
    AND h.status IN (700,900,701,901)
    AND (TRIM(h.ref_order_type) IN ('JYCK','HHCK','BFCK','QTCK') OR TRIM(h.ref_order_type) LIKE 'SO%')
    AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
    AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header so
      WHERE TRIM(so.company_code)=TRIM(h.company_code) AND TRIM(so.sales_order_code)=TRIM(h.ref_order_code)
        AND TRIM(so.sales_order_code)<>'' AND COALESCE(so.deleted_flag,0)=0
        AND IFNULL(so._fdl_marked_deleted,'false')='false')
), ledger AS (
  SELECT h.owner, h.document, h.warehouse, h.ship_at, TRIM(d.sku_code) AS sku,
    SUM(d.ship_qty) AS quantity,
    SUM(CASE WHEN d.ship_qty IS NULL OR d.ship_qty<0 THEN 1 ELSE 0 END) AS invalid_quantity
  FROM eligible h JOIN ywzt_ods.scm_sales_shipment_detail d
    ON TRIM(d.company_code)=h.owner AND TRIM(d.outbound_code)=h.document
  WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
  GROUP BY h.owner,h.document,h.warehouse,h.ship_at,TRIM(d.sku_code)
), documents AS (SELECT DISTINCT owner,document FROM ledger), conflicts AS (
  SELECT h.owner,h.document FROM eligible h
  WHERE EXISTS (SELECT 1 FROM documents d WHERE d.owner=h.owner AND d.document=h.document)
  GROUP BY h.owner,h.document HAVING COUNT(*)<>1
)
SELECT COALESCE(SUM(quantity),0) AS quantity, COUNT(*) AS detail_count,
  (SELECT COUNT(*) FROM documents) AS document_count,
  (SELECT COUNT(*) FROM conflicts) AS header_conflicts,
  COALESCE(SUM(invalid_quantity),0) AS invalid_quantity,
  COALESCE(SUM(CASE WHEN COALESCE(owner,'')='' OR COALESCE(document,'')='' OR COALESCE(sku,'')=''
    OR COALESCE(warehouse,'')='' THEN 1 ELSE 0 END),0) AS invalid_identity,
  MAX(ship_at) AS latest_shipment_at
FROM ledger"""


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing reference number")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid reference number")
    return result


def count(value):
    value = number(value)
    if value != value.to_integral_value():
        raise ValueError("invalid reference count")
    return int(value)


def reference(rows):
    if len(rows) != 1:
        raise ValueError("reference scalar required")
    row = rows[0]
    if any(count(row[key]) for key in ("header_conflicts", "invalid_quantity", "invalid_identity")):
        raise ValueError("reference source quality failed")
    return {"quantity": number(row["quantity"]), "detail_count": count(row["detail_count"]),
            "document_count": count(row["document_count"]), "latest_shipment_at": str(row["latest_shipment_at"])}


def verify_rows(rows, control, *, start, end, warehouse=None):
    keys, documents = set(), set()
    quantity = Fraction(0)
    for row in rows:
        key = tuple(row[field] for field in ("cargo_owner", "outbound_code", "sku_code"))
        warehouse_code = row["warehouse_code"]
        if key in keys or not all(isinstance(value, str) and value and value.strip() == value for value in (*key, warehouse_code)):
            raise ValueError("invalid detail identity")
        if warehouse is not None and warehouse_code != warehouse:
            raise ValueError("detail outside requested warehouse")
        stamp = datetime.fromisoformat(row["ship_at"])
        if not start <= stamp.date() < end:
            raise ValueError("detail outside requested interval")
        keys.add(key)
        documents.add(key[:2])
        quantity += Fraction(number(row["quantity"]))
    return (len(keys) == control["detail_count"] and len(documents) == control["document_count"]
            and quantity == Fraction(control["quantity"]))


def is_capacity_summary(table, control):
    return (table.get("format") == "table"
            and {column.get("key") for column in table.get("columns", [])} == {"quantity", "outbound_count"}
            and len(table.get("columns", [])) == 2 and not table.get("pagination")
            and len(table.get("rows", [])) == 1 and control["detail_count"] > 100000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--monthly-partitions", action="store_true", help="exercise the explicit monthly execution wording")
    parser.add_argument("--warehouse", help="explicit registered warehouse code; omitted means ALL")
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        parser.error("days must be between 1 and 3650")
    if args.warehouse is not None and (not args.warehouse.strip() or len(args.warehouse) > 40):
        parser.error("warehouse must be a nonempty code of at most 40 characters")
    _hide_windows_subprocesses()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = today - timedelta(days=args.days)
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    sql = independent_control_sql(start, today, warehouse=args.warehouse)

    def read_control():
        return reference(validate_doris_result(bridge.query(sql=sql, purpose="independent sales detail window controls")).rows)

    before = read_control()
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    body = {"conversation_id": session["conversation_id"],
            "question": ("按月拆分查询" if args.monthly_partitions else "查询") + f"近{args.days}个完整自然日{args.warehouse or ''}实际销售出库明细，用表格展示。"}
    request_key = "sales-detail-acceptance-" + uuid4().hex
    task = client.request("/v1/tasks", body, session["csrf_token"], request_key)
    identifier = task["task_id"]
    print(json.dumps({"task_id": identifier, "state": task["state"]}), flush=True)
    started = time.monotonic()
    while task["state"] in {"queued", "running"} and time.monotonic() - started < 300:
        time.sleep(2)
        task = client.request("/v1/tasks/" + identifier)
    if task["state"] != "completed":
        print(json.dumps({"task_id": identifier, "state": task["state"], "result_state": task.get("result", {}).get("result_state")}), flush=True)
        return 2
    result = task["result"]
    table = result.get("business_table") or {}
    state = result.get("result_state")
    expected_state = "summary_only" if before["detail_count"] > 100000 else "live_success" if before["detail_count"] else "no_data"
    checks = {"expected_result_state": state == expected_state,
              "no_repeated_clarification": result.get("clarification", {}).get("required") is False}
    if state == "summary_only":
        checks["capacity_summary_is_not_detail"] = is_capacity_summary(table, before)
        row = table["rows"][0]
        checks["independent_summary_matches"] = (number(row["quantity"]) == before["quantity"]
                                                  and count(row["outbound_count"]) == before["document_count"])
    else:
        expected_columns = {"cargo_owner", "warehouse_code", "outbound_code", "sku_code", "ship_at", "quantity"}
        assert table.get("format") == "table"
        pages = table.get("pagination")
        rows = []
        page_count = pages["page_count"] if pages else 1
        assert type(page_count) is int and 1 <= page_count <= 1000
        for index in range(1, page_count + 1):
            page = table if index == 1 else client.request(f'/v1/results/{pages["result_id"]}/pages/{index}')["business_table"]
            assert {column["key"] for column in page["columns"]} == expected_columns
            assert page["caption"] == table["caption"] and page["summary"] == table["summary"]
            assert len(page["rows"]) <= 100
            if pages:
                meta = page["pagination"]
                assert meta["reconciled"] is True and meta["result_id"] == pages["result_id"]
                assert meta["total_rows"] == before["detail_count"] and meta["page_count"] == page_count
                assert meta["page"] == index and meta["start_row"] == len(rows) + 1
                assert meta["end_row"] == len(rows) + len(page["rows"])
            rows.extend(page["rows"])
        checks["complete_rows_and_controls_match"] = verify_rows(rows, before, start=start, end=today, warehouse=args.warehouse)
        checks["date_and_identity_checks"] = True
    after = read_control()
    replay = client.request("/v1/tasks", body, session["csrf_token"], request_key)
    checks["stable_independent_controls"] = before == after
    checks["idempotent_replay"] = replay["task_id"] == identifier and replay.get("result") == result
    checks["calendar_anchor_unchanged"] = datetime.now(ZoneInfo("Asia/Shanghai")).date() == today
    wire = json.dumps(result, ensure_ascii=False).casefold()
    checks["no_internal_sql_or_sources"] = not any(marker in wire for marker in ("select ", "ywzt_ods.", "traceback (", "password="))
    print(json.dumps({"case": "sales_detail_window", "days": args.days, "result_state": state,
                      "monthly_partitions": args.monthly_partitions,
                      "scope": "explicit_warehouse" if args.warehouse else "ALL",
                      "checks": checks, "elapsed_seconds": round(time.monotonic() - started, 2)}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"state": "verification_failed", "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(2) from None
