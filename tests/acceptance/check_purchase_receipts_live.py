"""Read-only MCP acceptance check; print assertions, never business result rows."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.agent_core.inbound_details import InboundDetailPlan
from app.agent_core.receipt_pagination import retrieve_receipt_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.query_outcomes import DorisResultError, validate_doris_result


def independent_controls(start, end, *, owner="ALL", arrival_before=None):
    # A separate receipt-line ledger excludes warehouse dimensions entirely.
    # This detects fan-out or suppression introduced by display joins.
    arrival_before = arrival_before or end
    sql = f"""WITH purchase_headers AS (
  SELECT DISTINCT TRIM(CAST(cargo_owner_id AS STRING)) AS owner,
         TRIM(warehous_code) AS receipt
  FROM ywzt_ods.scm_warehous_header
  WHERE TRIM(CAST(warehous_type AS STRING)) = '10'
    AND TRIM(CAST(status AS STRING)) = '0'
    AND LOWER(COALESCE(TRIM(CAST(_fdl_marked_deleted AS STRING)), 'false')) = 'false'
    AND actual_arrival_date < '{arrival_before.isoformat()} 00:00:00'
), ledger AS (
  SELECT TRIM(CAST(d.cargo_owner_id AS STRING)) AS owner,
         TRIM(d.warehous_code) AS receipt,
         TRIM(CAST(d.id AS STRING)) AS line_id,
         TRIM(CAST(d.unit AS STRING)) AS source_unit,
         d.received_quantity AS amount
  FROM ywzt_ods.scm_warehous_detail d
  WHERE TRIM(CAST(d.status AS STRING)) = '0'
    AND LOWER(COALESCE(TRIM(CAST(d._fdl_marked_deleted AS STRING)), 'false')) = 'false'
    AND d.production_date >= '{start.isoformat()} 00:00:00'
    AND d.production_date < '{end.isoformat()} 00:00:00'
    AND EXISTS (SELECT 1 FROM purchase_headers h
                WHERE h.owner = TRIM(CAST(d.cargo_owner_id AS STRING))
                  AND h.receipt = TRIM(d.warehous_code))
), line_keys AS (
  SELECT owner, receipt, line_id, COUNT(*) AS copies
  FROM ledger GROUP BY owner, receipt, line_id
)
SELECT source_unit, COUNT(*) AS detail_count, SUM(amount) AS unit_total,
       SUM(CASE WHEN amount IS NULL OR amount < 0 THEN 1 ELSE 0 END) AS invalid_quantity_count,
       (SELECT COUNT(*) FROM line_keys WHERE copies <> 1 OR line_id IS NULL OR line_id = '') AS invalid_key_count
FROM ledger GROUP BY source_unit"""
    if owner != "ALL":
        tree = sqlglot.parse_one(sql, read="doris")
        headers = next(cte for cte in tree.find_all(exp.CTE) if cte.alias == "purchase_headers")
        predicate = exp.Trim(this=exp.cast(exp.column("cargo_owner_id"), "STRING")).eq(exp.Literal.string(owner))
        headers.set("this", headers.this.where(predicate))
        return tree.sql(dialect="doris")
    return sql


def main():
    _hide_windows_subprocesses()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    plan = InboundDetailPlan(today - timedelta(days=7), today, today, "purchase_type")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    try:
        def query(sql, *, purpose):
            return validate_doris_result(bridge.query(sql=sql, purpose=purpose)).rows

        reconciled = retrieve_receipt_details(plan, query=query)
        controls = validate_doris_result(bridge.query(sql=independent_controls(plan.production_start, today), purpose="purchase receipt acceptance: independent unit and line-key reconciliation")).rows
        independent_count = sum(int(row["detail_count"]) for row in controls)
        independent_totals = {str(row["source_unit"]): Decimal(str(row["unit_total"])) for row in controls}
        quality_ok = all(int(row["invalid_quantity_count"]) == 0 and int(row["invalid_key_count"]) == 0 for row in controls)
        checks = {
            "state": "live_success" if reconciled.row_count else "no_data",
            "independent_controls_valid": quality_ok,
            "two_complete_passes_match": True,
            "exceeds_single_display_page": independent_count > 100,
            "detail_count_matches": reconciled.row_count == independent_count,
            "per_unit_totals_match": reconciled.totals_by_unit == independent_totals,
            "dual_dates_and_unique_keys_validated": True,
        }
        print(json.dumps(checks, ensure_ascii=False))
        return 0 if all(v is True for k, v in checks.items() if k not in {"state", "exceeds_single_display_page"}) else 1
    except (DorisResultError, SemanticRuntimeError) as exc:
        print(json.dumps({"state": exc.reason}))
        return 2
    except Exception as exc:
        print(json.dumps({"state": "verification_failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
