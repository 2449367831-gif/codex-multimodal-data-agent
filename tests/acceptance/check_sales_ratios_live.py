"""Reconcile existing generic sales-ratio tasks through independent MCP reads."""

import argparse
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import sys

import sqlglot

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient
from tests.acceptance.check_composed_operators_live import column, number
from tests.acceptance.check_outbound_boxes_live import reference_sql
from tests.acceptance.check_partition_ranking_live import complete_rows


def independent_sql(case, start, end, top_n=3):
    assert case in {"daily_average", "partition_share"}, "unknown_case"
    assert type(start) is date and type(end) is date and start < end, "invalid_period"
    assert type(top_n) is int and 1 <= top_n <= 100, "invalid_rank_limit"
    tree = sqlglot.parse_one(reference_sql(start, end, all_actual=True, finished_goods=False), read="doris")
    ctes = [cte.sql(dialect="doris") for cte in tree.args["with_"].expressions
            if cte.alias_or_name in {"distinct_headers", "customer_types", "raw_facts", "facts"}]
    if case == "daily_average":
        calculation = """doc_totals AS (
 SELECT day,owner,doc,SUM(quantity) quantity FROM facts GROUP BY day,owner,doc
), expected AS (
 SELECT day,SUM(quantity) quantity,COUNT(*) documents FROM doc_totals GROUP BY day
)"""
        order = "day"
    else:
        calculation = f"""sku_totals AS (
 SELECT owner,warehouse,sku,SUM(quantity) quantity FROM facts GROUP BY owner,warehouse,sku
), ranked AS (
 SELECT *,SUM(quantity) OVER (PARTITION BY owner,warehouse) warehouse_quantity,
 ROW_NUMBER() OVER (PARTITION BY owner,warehouse ORDER BY quantity DESC,sku ASC) ranking
 FROM sku_totals
), expected AS (SELECT * FROM ranked WHERE ranking<={top_n})"""
        order = "owner,warehouse,ranking"
    return "WITH " + ",".join(ctes) + "," + calculation + f""", quality AS (
 SELECT COUNT(*)-COUNT(quantity) missing_quantity,
 COALESCE(SUM(CASE WHEN quantity<0 THEN 1 ELSE 0 END),0) negative_quantity,
 COALESCE(SUM(CASE WHEN COALESCE(owner,'')='' OR COALESCE(warehouse,'')='' OR COALESCE(sku,'')=''
 OR COALESCE(doc,'')='' THEN 1 ELSE 0 END),0) missing_key FROM facts
)
SELECT e.*,q.*,(SELECT COUNT(*) FROM expected) expected_rows
FROM expected e CROSS JOIN quality q ORDER BY {order} LIMIT 100001"""


def compare(case, table, rows, reference):
    assert rows and reference, "nonempty_evidence_required"
    assert all(number(r["expected_rows"]) == len(reference) for r in reference), "incomplete_reference"
    assert all(number(r[k]) == 0 for r in reference for k in
               ("missing_quantity", "negative_quantity", "missing_key")), "source_quality_failed"
    quantity = column(table, ("实际销售出库数量", "实际销售出库", "实际销售出库量", "实际出库数量", "实发数量", "出库数量"), "EA")
    if case == "daily_average":
        identity = [column(table, ("出库日期", "日期", "统计日期"))]
        source_identity = ("day",)
        documents = column(table, ("出库单数", "去重出库单数"), "单")
        ratio = column(table, ("每单平均出库量", "单均出库数量", "每单平均实发量", "单均出库量", "每单平均出库数量"), "EA/单")
    else:
        identity = [column(table, labels) for labels in
                    (("货主", "货主编码"), ("仓库编码",), ("SKU", "SKU编码", "商品编码"))]
        source_identity = ("owner", "warehouse", "sku")
        ranking = column(table, ("排名", "仓内排名", "仓库内排名"))
        ratio = column(table, ("本仓实发数量占比", "仓内数量占比", "占本仓数量比例", "占本仓实发比例", "实发数量占比", "数量占比"), "%")
    expected = {tuple(str(r[k]) for k in source_identity): r for r in reference}
    actual = {tuple(str(r[k]) for k in identity): r for r in rows}
    assert len(expected) == len(reference) and len(actual) == len(rows), "duplicate_group"
    assert actual.keys() == expected.keys(), "group_mismatch"
    with localcontext() as context:
        context.prec = 100
        for key, row in actual.items():
            control = expected[key]
            assert number(row[quantity]) == number(control["quantity"]), "quantity_mismatch"
            if case == "daily_average":
                assert number(row[documents]) == number(control["documents"]), "document_mismatch"
                denominator = number(control["documents"])
                assert denominator > 0 and denominator == denominator.to_integral_value(), "invalid_document_count"
                value = number(control["quantity"]) / denominator
            else:
                assert number(row[ranking]) == number(control["ranking"]), "partition_rank_mismatch"
                denominator = number(control["warehouse_quantity"])
                if denominator == 0:
                    assert row[ratio] is None, "zero_denominator_replaced"
                    continue
                assert denominator > 0, "invalid_warehouse_quantity"
                value = number(control["quantity"]) / denominator * 100
            assert abs(number(row[ratio]) - value) <= Decimal("0.005"), "ratio_mismatch"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--case", choices=("daily_average", "partition_share"), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--top-n", type=int, default=3)
    args = parser.parse_args()
    assert re.fullmatch(r"task_[a-f0-9]{32}", args.task_id), "invalid_task"
    sql = independent_sql(args.case, args.start, args.end, args.top_n)
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    client.request("/v1/session")
    task = client.request("/v1/tasks/" + args.task_id)
    assert task["state"] == "completed" and task["result"]["result_state"] == "live_success", "not_live_result"
    table = task["result"]["business_table"]
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    before = validate_doris_result(bridge.query(sql=sql, purpose="independent sales ratio reference")).rows
    rows = complete_rows(client, table)
    compare(args.case, table, rows, before)
    after = validate_doris_result(bridge.query(sql=sql, purpose="independent sales ratio source stability")).rows
    assert before == after, "source_changed"
    print(json.dumps({"task_id": args.task_id, "case": args.case, "verification": "passed",
                      "rows": len(rows), "checks": ["all_groups", "quantity", "denominator", "ratio", "units", "source_stability"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}))
        raise SystemExit(1) from None
