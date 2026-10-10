"""Independent all-scope live turnover check; no business rows are persisted."""

import argparse
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient
from tests.acceptance.check_composed_operators_live import number, column
from tests.acceptance.check_partition_ranking_live import complete_rows


def reference_sql(start, end):
    assert type(start) is date and type(end) is date and 0 < (end - start).days <= 366
    return f"""WITH products AS (
 SELECT DISTINCT TRIM(sku_code) sku FROM ywzt_ods.master_sku WHERE TRIM(sku_group)='50'
), inventory_by_warehouse AS (
 SELECT TRIM(i.owner) owner,TRIM(i.wms_desc) warehouse,
 SUM(COALESCE(i.available_quantity,0)+COALESCE(i.in_transit_quantity,0)) qty, COUNT(*) facts
 FROM ywzt_ods.sku_inventory i WHERE EXISTS (SELECT 1 FROM products p WHERE p.sku=TRIM(i.sku_code))
 GROUP BY TRIM(i.owner),TRIM(i.wms_desc)
), inventory AS (
 SELECT SUM(qty) qty,SUM(facts) facts,SUM(CASE WHEN qty<0 THEN 1 ELSE 0 END) negative_warehouses
 FROM inventory_by_warehouse
), documents AS (
 SELECT DISTINCT TRIM(h.company_code) owner,TRIM(h.outbound_code) doc
 FROM ywzt_ods.scm_sales_shipment_header h
 WHERE h.ship_at>='{start} 00:00:00' AND h.ship_at<'{end} 00:00:00'
 AND h.status IN (700,900,701,901)
 AND (TRIM(h.ref_order_type) IN ('JYCK','HHCK','BFCK','QTCK') OR TRIM(h.ref_order_type) LIKE 'SO%')
 AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
 AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header o
 WHERE TRIM(o.company_code)=TRIM(h.company_code) AND TRIM(o.sales_order_code)=TRIM(h.ref_order_code)
 AND TRIM(o.sales_order_code)<>'' AND COALESCE(o.deleted_flag,0)=0 AND IFNULL(o._fdl_marked_deleted,'false')='false')
), sales AS (
 SELECT SUM(d.ship_qty) qty, COUNT(*) facts,
 SUM(CASE WHEN d.ship_qty IS NULL OR d.ship_qty<0 THEN 1 ELSE 0 END) bad_quantity
 FROM ywzt_ods.scm_sales_shipment_detail d
 WHERE EXISTS (SELECT 1 FROM documents h WHERE h.owner=TRIM(d.company_code) AND h.doc=TRIM(d.outbound_code))
 AND EXISTS (SELECT 1 FROM products p WHERE p.sku=TRIM(d.sku_code))
 AND COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
)
SELECT i.qty inventory_qty,s.qty sales_qty,i.facts inventory_facts,s.facts sales_facts,s.bad_quantity,
 i.negative_warehouses
FROM inventory i CROSS JOIN sales s"""


def compare(table, rows, reference, days):
    assert len(rows) == len(reference) == 1, "scalar_grain"
    row, control = rows[0], reference[0]
    assert number(control["inventory_facts"]) > 0 and number(control["sales_facts"]) > 0, "missing_reference_facts"
    assert number(control["bad_quantity"]) == 0, "invalid_sales_facts"
    assert number(control["negative_warehouses"]) == 0, "negative_inventory_warehouse"
    inventory, sales = number(control["inventory_qty"]), number(control["sales_qty"])
    assert inventory >= 0 and sales > 0, "invalid_reference_denominator"
    with localcontext() as context:
        context.prec = 100
        expected = (
            (("完整自然日数", "完整自然日", "统计天数"), "个完整自然日", Decimal(days)),
            (("当前可用成品库存加在途", "当前可用成品库存含在途", "当前可用成品库存", "成品库存含在途"), "EA", inventory),
            (("实际销售出库成品数量", "成品实际销售出库量", "实际销售出库量"), "EA", sales),
            (("实际销售出库成品日均量", "实际销售出库成品日均数量", "成品日均销量", "实际销售日均量"), "EA/日", sales / days),
            (("原始周转天数", "库存周转天数", "周转天数"), "天", inventory / (sales / days)),
            (("两倍周转天数", "原始周转天数两倍"), "天", inventory / (sales / days) * 2),
        )
        for labels, unit, value in expected:
            actual = number(row[column(table, labels, unit)])
            assert abs(actual - value) <= Decimal("0.005"), "independent_measure_mismatch"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    assert re.fullmatch(r"task_[a-f0-9]{32}", args.task_id), "invalid_task_id"
    sql = reference_sql(args.start, args.end)
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    client.request("/v1/session")
    task = client.request("/v1/tasks/" + args.task_id)
    assert task["state"] == "completed" and task["result"]["result_state"] == "live_success", "not_live_result"
    table = task["result"]["business_table"]
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    before = validate_doris_result(bridge.query(sql=sql, purpose="independent derived turnover reference")).rows
    compare(table, complete_rows(client, table), before, (args.end - args.start).days)
    after = validate_doris_result(bridge.query(sql=sql, purpose="independent derived turnover stability")).rows
    assert before == after, "source_changed"
    print(json.dumps({"task_id": args.task_id, "verification": "passed",
                      "checks": ["inventory_basis", "actual_shipped_quantity", "calendar_days", "ratio", "derived_ratio", "units", "source_stability"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}))
        raise SystemExit(1) from None
