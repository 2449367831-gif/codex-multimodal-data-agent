"""Reconcile existing comparison/filter tasks without submitting another query.

Business values stay in memory. References use only the approved MCP bridge and
the independent source ledger, not the production model or SQL compiler.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
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
from tests.acceptance.check_outbound_boxes_live import reference_sql
from tests.acceptance.check_partition_ranking_live import complete_rows


def number(value):
    assert not isinstance(value, bool) and isinstance(value, (str, int, float, Decimal)), "invalid_number"
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise AssertionError("invalid_number") from None
    assert result.is_finite(), "invalid_number"
    return result


def independent_sql(case, start, end, *, second=None, threshold=Decimal(1000)):
    assert type(start) is date and type(end) is date and start < end, "invalid_reference_period"
    if case == "period":
        assert second and all(type(d) is date for d in second) and second[0] < second[1], "invalid_second_period"
        lower, upper = min(start, second[0]), max(end, second[1])
    else:
        assert case == "threshold", "invalid_case"
        lower, upper = start, end
        threshold = number(threshold)
        assert 0 <= threshold <= 10**15, "invalid_threshold"
    tree = sqlglot.parse_one(reference_sql(lower, upper, all_actual=True, finished_goods=False), read="doris")
    ctes = [cte.sql(dialect="doris") for cte in tree.args["with_"].expressions
            if cte.alias_or_name in {"distinct_headers", "customer_types", "raw_facts", "facts"}]
    if case == "threshold":
        eligible = "SELECT * FROM facts"
        grouped = f"""SELECT owner,warehouse,sku,SUM(quantity) quantity
 FROM eligible GROUP BY owner,warehouse,sku HAVING SUM(quantity) > {threshold}"""
        order = "e.owner,e.warehouse,e.sku"
    else:
        left = f"day >= '{start}' AND day < '{end}'"
        right = f"day >= '{second[0]}' AND day < '{second[1]}'"
        eligible = f"SELECT * FROM facts WHERE ({left}) OR ({right})"
        grouped = f"""SELECT owner,warehouse,
 SUM(CASE WHEN {left} THEN quantity END) left_quantity,
 SUM(CASE WHEN {right} THEN quantity END) right_quantity,
 SUM(CASE WHEN {left} THEN 1 ELSE 0 END) left_records,
 SUM(CASE WHEN {right} THEN 1 ELSE 0 END) right_records
 FROM eligible GROUP BY owner,warehouse"""
        order = "e.owner,e.warehouse"
    return "WITH " + ",".join(ctes) + f""", eligible AS ({eligible}), expected AS ({grouped}), quality AS (
 SELECT COUNT(*)-COUNT(quantity) missing_quantity,
 COALESCE(SUM(CASE WHEN quantity<0 THEN 1 ELSE 0 END),0) negative_quantity,
 COALESCE(SUM(CASE WHEN COALESCE(owner,'')='' OR COALESCE(warehouse,'')='' OR COALESCE(sku,'')='' THEN 1 ELSE 0 END),0) missing_key
 FROM eligible
)
SELECT e.*,q.*,(SELECT COUNT(*) FROM expected) expected_rows
FROM expected e CROSS JOIN quality q ORDER BY {order} LIMIT 100001"""


def reference_groups(reference, dimensions):
    groups = {}
    for row in reference:
        assert number(row["expected_rows"]) == len(reference), "incomplete_reference"
        assert all(number(row[k]) == 0 for k in ("missing_quantity", "negative_quantity", "missing_key")), "reference_quality_failed"
        key = tuple(row[k] for k in dimensions)
        assert all(isinstance(k, str) and k.strip() for k in key) and key not in groups, "invalid_reference_identity"
        groups[key] = row
    return groups


def column(table, labels, unit=None):
    matches = [c for c in table["columns"] if c["label"] in labels]
    assert len(matches) == 1, "missing_or_ambiguous_column"
    descriptor = matches[0]
    if unit is not None:
        assert descriptor.get("unit") == unit and descriptor.get("kind") == "number", "wrong_measure_unit"
    return descriptor["key"]


def public_groups(table, rows, dimensions):
    keys = [column(table, labels) for labels in dimensions]
    groups = {}
    for row in rows:
        key = tuple(row[k] for k in keys)
        assert all(isinstance(k, str) and k.strip() for k in key) and key not in groups, "invalid_public_identity"
        groups[key] = row
    return groups


IDENTITIES = (("货主编码", "货主"), ("仓库编码",))


def compare_threshold(table, rows, reference, threshold):
    expected = reference_groups(reference, ("owner", "warehouse", "sku"))
    actual = public_groups(table, rows, (*IDENTITIES, ("SKU编码", "SKU", "商品编码")))
    quantity = column(table, ("实发数量", "实际销售出库量", "实际出库数量", "出库数量"), "EA")
    assert actual.keys() == expected.keys(), "filtered_group_mismatch"
    for key, row in actual.items():
        assert number(row[quantity]) == number(expected[key]["quantity"]), "filtered_quantity_mismatch"
        assert number(row[quantity]) > number(threshold), "post_aggregate_filter_mismatch"


def period_labels(start, end, index, suffix):
    last = end - timedelta(days=1)
    date_label = f"{start.month}月{start.day}至{last.day}日" if start.month == last.month else f"{start.month}月{start.day}日至{last.month}月{last.day}日"
    return (f"{date_label}{suffix}", f"期间{index}{suffix}", f"期间{'一' if index == 1 else '二'}{suffix}")


def compare_periods(table, rows, reference, periods):
    expected = reference_groups(reference, ("owner", "warehouse"))
    actual = public_groups(table, rows, IDENTITIES)
    assert actual.keys() == expected.keys(), "period_union_mismatch"
    averages = [column(table, period_labels(a, b, i, "日均量"), "EA/日") for i, (a, b) in enumerate(periods, 1)]
    states = [column(table, period_labels(a, b, i, "记录状态")) for i, (a, b) in enumerate(periods, 1)]
    difference = column(table, ("日均量差额", "日均差额"), "EA/日")
    with localcontext() as context:
        context.prec = 100
        for key, row in actual.items():
            controls = expected[key]
            expected_averages = []
            for i, side in enumerate(("left", "right")):
                count = number(controls[side + "_records"])
                assert count >= 0 and count == count.to_integral_value(), "invalid_record_count"
                if count == 0:
                    assert controls[side + "_quantity"] is None, "inconsistent_reference_missingness"
                    assert row[averages[i]] is None and row[states[i]] == "无记录", "missing_records_replaced"
                    expected_averages.append(None)
                else:
                    average = number(controls[side + "_quantity"]) / (periods[i][1] - periods[i][0]).days
                    assert abs(number(row[averages[i]]) - average) <= Decimal("0.005"), "wrong_period_average"
                    assert row[states[i]] == "有记录", "zero_misclassified_as_missing"
                    expected_averages.append(average)
            if any(v is None for v in expected_averages):
                assert row[difference] is None, "missing_difference_replaced"
            else:
                assert abs(number(row[difference]) - (expected_averages[1] - expected_averages[0])) <= Decimal("0.005"), "wrong_average_difference"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--case", choices=("period", "threshold"), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--second-start", type=date.fromisoformat)
    parser.add_argument("--second-end", type=date.fromisoformat)
    parser.add_argument("--threshold", type=Decimal, default=Decimal(1000))
    args = parser.parse_args()
    if not re.fullmatch(r"task_[a-f0-9]{32}", args.task_id):
        parser.error("invalid task ID")
    second = (args.second_start, args.second_end) if args.case == "period" else None
    sql = independent_sql(args.case, args.start, args.end, second=second, threshold=args.threshold)
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    client.request("/v1/session")
    task = client.request("/v1/tasks/" + args.task_id)
    assert task["state"] == "completed" and task["result"]["result_state"] == "live_success", "task_not_live"
    table = task["result"]["business_table"]
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    reference = validate_doris_result(bridge.query(sql=sql, purpose="independent composed operators reference")).rows
    rows = complete_rows(client, table)
    if args.case == "period":
        compare_periods(table, rows, reference, ((args.start, args.end), second))
    else:
        compare_threshold(table, rows, reference, args.threshold)
    after = validate_doris_result(bridge.query(sql=sql, purpose="independent composed operators stability")).rows
    assert after == reference, "source_changed"
    print(json.dumps({"task_id": args.task_id, "case": args.case, "verification": "passed",
                      "checks": ["all_groups", "independent_values", "units", "source_stability"]}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
