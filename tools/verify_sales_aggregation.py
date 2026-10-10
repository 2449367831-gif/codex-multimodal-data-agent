"""Verify T01/T02/T05 via the approved MCP tool; print checks, never result rows."""

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent_core.runtime import SemanticQueryRuntime
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.chat_api import _public_business_table


def reference_sql(request):
    """Independent direct-detail aggregate; duplicate sales orders use EXISTS."""
    dimensions = request.sales_aggregation.dimensions
    fields = []
    if "warehouse" in dimensions:
        fields += [("company_code", "cargo_owner"), ("facility_code", "warehouse_code")]
    if "ship_date" in dimensions:
        fields += [("DATE(ship_at)", "ship_date")]
    if "shipment_status" in dimensions:
        fields += [("CAST(status AS STRING)", "shipment_status")]
    selected = ", ".join(f"{expression} AS {alias}" for expression, alias in fields)
    keys = ", ".join(alias for _, alias in fields)
    owner = " AND TRIM(h.company_code) = 'HJXH'" if request.scope.owner == "HJXH" else ""
    return f"""WITH eligible AS (
  SELECT DISTINCT TRIM(h.company_code) AS company_code, TRIM(h.outbound_code) AS outbound_code,
    TRIM(h.facility_code) AS facility_code, h.ship_at, h.status
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE h.ship_at >= '{request.start.isoformat()} 00:00:00'
    AND h.ship_at < '{request.end.isoformat()} 00:00:00'
    AND h.status IN (700, 900, 701, 901)
    AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%')
    AND COALESCE(h.delete_flag,0) = 0 AND IFNULL(h._fdl_marked_deleted,'false') = 'false'{owner}
    AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header AS so
      WHERE TRIM(so.company_code) = TRIM(h.company_code) AND TRIM(so.sales_order_code) = TRIM(h.ref_order_code)
      AND TRIM(so.sales_order_code) <> '' AND COALESCE(so.deleted_flag,0) = 0
      AND IFNULL(so._fdl_marked_deleted,'false') = 'false')
), documents AS (
  SELECT h.company_code, h.outbound_code, h.facility_code, h.ship_at, h.status,
    SUM(COALESCE(d.ship_qty,0)) AS document_quantity
  FROM eligible AS h JOIN ywzt_ods.scm_sales_shipment_detail AS d
    ON TRIM(d.company_code) = h.company_code AND TRIM(d.outbound_code) = h.outbound_code
  WHERE COALESCE(d.delete_flag,0) = 0 AND IFNULL(d._fdl_marked_deleted,'false') = 'false'
  GROUP BY h.company_code, h.outbound_code, h.facility_code, h.ship_at, h.status
), keyed AS (
  SELECT {selected}, document_quantity FROM documents
)
SELECT {keys}, SUM(document_quantity) AS quantity, COUNT(*) AS outbound_count
FROM keyed GROUP BY {keys} ORDER BY {keys} LIMIT 501"""


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    runtime = SemanticQueryRuntime(query_client=bridge, clock=lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
    cases = json.loads((ROOT / "tests/acceptance/complex-query-cases.json").read_text(encoding="utf-8"))["cases"]
    successful = True
    for case in cases:
        if case["id"] not in {"T01", "T02", "T05"}:
            continue
        try:
            request = runtime.classify(case["question"])
            if request is None or request.route != "sales_aggregate":
                raise ValueError("reviewed_plan_missing")
            result = runtime.execute_plan(request, question=case["question"])
            table = _public_business_table(result["business_table"], live=True)
            reference = bridge.query(sql=reference_sql(request), purpose=f"independent {case['id']} group reconciliation")["execution"]["rows"]
            keys = request.sales_aggregation.keys
            key = lambda row: tuple(str(row[k]) for k in keys)
            actual = {key(row): (Decimal(row["quantity"]), int(row["outbound_count"])) for row in table["rows"]}
            control = {key(row): (Decimal(str(row["quantity"])), int(row["outbound_count"])) for row in reference}
            checks = {"live": result["query_execution"]["live_business_result_executed"],
                      "group_reconciliation": actual == control,
                      "complete_reference": len(reference) <= 500,
                      "no_clarification": not result["interaction"]["questions"]}
            successful &= all(checks.values())
            print(json.dumps({"case": case["id"], "checks": checks}, ensure_ascii=False), flush=True)
        except Exception as exc:
            successful = False
            print(json.dumps({"case": case["id"], "passed": False, "failure_type": type(exc).__name__,
                              "reason": getattr(exc, "reason", "verification_failed")}), flush=True)
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
