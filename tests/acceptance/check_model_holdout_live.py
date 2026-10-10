"""Exercise model-only compositions against an independent ledger, in memory."""

import argparse
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import sqlglot

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.planning import RequestPlanner
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient, _number
from tests.acceptance.check_outbound_boxes_live import reference_sql
from tests.acceptance.check_partition_ranking_live import complete_rows, records


LABELS = ["货主编码", "仓库编码", "首日实发数量", "次日实发数量", "实发增量"]
AVERAGE_LABELS = ["货主编码", "仓库编码", "实发数量", "货主平均仓库实发量", "货主内数量占比"]
COVERAGE_LABELS = ["货主编码", "仓库编码", "实发数量", "累计实发数量", "累计数量占比"]


def question(first, second):
    return (
        f"比较{first}与{second}两个完整自然日的实际销售出库，以北京时间为准。"
        "全部货主、全部仓库、全部商品和全部渠道，不限定成品，使用已发布实际销售出库口径。"
        "按货主编码和仓库编码保留身份，分别汇总两天实发数量；某天没有匹配记录时该天数量为0。"
        "只保留次日实发数量大于首日的仓库，实发增量等于次日减首日。"
        "按实发增量降序，同量时按货主编码、仓库编码升序。"
        "完整返回货主编码、仓库编码、首日实发数量、次日实发数量、实发增量五列业务表格，"
        "编码为文本，数量单位EA，不能省略仓库，不要展示查询语句或核对明细。"
    )


def average_question(first, second):
    return (
        f"查询北京时间{first}至{second}两个完整自然日的实际销售出库，全部货主、仓库、商品、渠道，"
        "不限定成品，使用已发布的实际销售出库口径。先按货主编码和仓库编码汇总这两天实发数量。"
        "对每个货主分别计算有匹配出库记录的仓库平均实发量，零数量但有记录的仓库也计入分母，"
        "没有记录的仓库不计入分母。仅保留实发数量严格大于本货主平均仓库实发量的仓库。"
        "再计算这些仓库占本货主全部有记录仓库实发总量的百分比，分母不能缩小为筛选后的仓库。"
        "分母为零时百分比为空。按货主编码升序、实发数量降序、仓库编码升序。"
        "完整返回货主编码、仓库编码、实发数量、货主平均仓库实发量、货主内数量占比五列业务表格，"
        "编码为文本，数量单位EA，占比单位%，平均量与占比保留两位小数。不要展示查询语句或内部核对明细。"
    )


def coverage_question(first, second):
    return (
        f"查询北京时间{first}至{second}两个完整自然日的实际销售出库，全部货主、仓库、商品和渠道，"
        "不限定成品，使用已发布实际销售出库口径，不沿用前一问的范围。"
        "先按货主编码和仓库编码汇总实发数量，只对汇总数量大于零的仓库做以下覆盖分析。"
        "每个货主独立按实发数量降序排列仓库，同量按仓库编码升序。"
        "找出按这个顺序累计覆盖该货主实发总量至少80%所需的最少仓库，"
        "必须包含第一次达到或超过80%的那一行，不保留它之后的仓库。"
        "分母为该货主所有正数量仓库的实发总量，不能用筛选后仓库的总量，"
        "必须用未四舍五入的累计值判定是否达到80%。"
        "结果按货主编码升序、实发数量降序、仓库编码升序，完整返回货主编码、仓库编码、"
        "实发数量、累计实发数量、累计数量占比五列业务表格。"
        "编码为文本，数量单位EA，占比单位%，占比保留两位小数。"
        "不要展示查询语句、数据库字段或内部核对明细。"
    )


def ledger_sql(first, second):
    tree = sqlglot.parse_one(reference_sql(first, second + timedelta(days=1),
                                          all_actual=True, finished_goods=False), read="doris")
    ctes = [cte.sql(dialect="doris") for cte in tree.args["with_"].expressions
            if cte.alias_or_name in {"distinct_headers", "customer_types", "raw_facts", "facts"}]
    return "WITH " + ",".join(ctes) + """
SELECT owner,warehouse,CAST(day AS STRING) day,SUM(quantity) quantity,
 COUNT(*) fact_count,COUNT(quantity) quantity_count
FROM facts GROUP BY owner,warehouse,day ORDER BY owner,warehouse,day"""


def expected(rows, first, second):
    grouped = defaultdict(lambda: [Decimal(0), Decimal(0)])
    seen = set()
    for row in rows:
        assert row["day"] in {str(first), str(second)}, "reference_date_invalid"
        identity = (row["owner"], row["warehouse"])
        assert all(isinstance(v, str) and v for v in identity), "reference_identity_invalid"
        fact = (*identity, row["day"])
        assert fact not in seen, "reference_duplicate"
        seen.add(fact)
        assert _number(row["fact_count"]) == _number(row["quantity_count"]), "source_quantity_missing"
        grouped[identity][int(row["day"] == str(second))] = _number(row["quantity"])
    result = [(*key, a, b, b-a) for key, (a, b) in grouped.items() if b > a]
    return sorted(result, key=lambda row: (-row[-1], row[0], row[1]))


def expected_above_average(rows, first, second):
    expected(rows, first, second)  # Verify source dates, identity and completeness.
    warehouses = defaultdict(Decimal)
    for row in rows:
        warehouses[(row["owner"], row["warehouse"])] += _number(row["quantity"])
    totals, counts = defaultdict(Decimal), defaultdict(int)
    for (owner, _), quantity in warehouses.items():
        totals[owner] += quantity
        counts[owner] += 1
    result = []
    for (owner, warehouse), quantity in warehouses.items():
        average = totals[owner] / counts[owner]
        if quantity > average:
            result.append((owner, warehouse, quantity, average,
                           quantity / totals[owner] * 100 if totals[owner] else None))
    return sorted(result, key=lambda row: (row[0], -row[2], row[1]))


def expected_coverage(rows, first, second):
    expected(rows, first, second)
    quantities = defaultdict(Decimal)
    for row in rows:
        quantities[row['owner'], row['warehouse']] += _number(row['quantity'])
    owners = defaultdict(list)
    for (owner, warehouse), quantity in quantities.items():
        if quantity > 0:
            owners[owner].append((warehouse, quantity))
    result = []
    for owner in sorted(owners):
        warehouses = sorted(owners[owner], key=lambda row: (-row[1], row[0]))
        total = sum((quantity for _, quantity in warehouses), Decimal(0))
        cumulative = Decimal(0)
        for warehouse, quantity in warehouses:
            if cumulative * 100 >= total * 80:
                break
            cumulative += quantity
            result.append((owner, warehouse, quantity, cumulative, cumulative / total * 100))
    return result


def verify_coverage(table, rows, baseline):
    assert table.get('table_id') == 'model_verified', 'not_model_execution'
    output = records(table, rows, COVERAGE_LABELS)
    columns = {column['label']: column for column in table['columns']}
    assert all(columns[label]['kind'] == 'text' for label in COVERAGE_LABELS[:2]), 'identity_type'
    for label, unit in zip(COVERAGE_LABELS[2:], ('EA', 'EA', '%')):
        assert columns[label]['kind'] == 'number' and columns[label].get('unit') == unit, 'quantity_type_or_unit'
    assert len(output) == len(baseline), 'incorrect_coverage_population'
    for actual, wanted in zip(output, baseline):
        assert tuple(actual[:2]) == wanted[:2], 'incorrect_coverage_membership_or_sort'
        assert tuple(_number(value) for value in actual[2:4]) == wanted[2:4], 'incorrect_coverage_quantity'
        assert abs(_number(actual[4]) - wanted[4]) <= Decimal('0.005'), 'incorrect_preslice_share'
    wire = json.dumps(table, ensure_ascii=False).casefold()
    assert not any(x in wire for x in ('select ', 'ywzt_ods.', 'traceback (', 'password=')), 'internal_disclosure'


def verify_above_average(table, rows, baseline):
    assert table.get("table_id") == "model_verified", "not_model_execution"
    output = records(table, rows, AVERAGE_LABELS)
    assert len(output) == len(baseline), "incorrect_above_average_population"
    columns = {c["label"]: c for c in table["columns"]}
    assert all(columns[label]["kind"] == "text" for label in AVERAGE_LABELS[:2]), "identity_type"
    for label, unit in zip(AVERAGE_LABELS[2:], ("EA", "EA", "%")):
        assert columns[label]["kind"] == "number" and columns[label].get("unit") == unit, "quantity_type_or_unit"
    for observed, wanted in zip(output, baseline):
        assert tuple(observed[:2]) == wanted[:2], "incorrect_group_membership_or_sort"
        assert _number(observed[2]) == wanted[2], "incorrect_quantity"
        assert abs(_number(observed[3]) - wanted[3]) <= Decimal("0.005"), "incorrect_owner_average"
        assert ((observed[4] is None) if wanted[4] is None else
                abs(_number(observed[4]) - wanted[4]) <= Decimal("0.005")), "incorrect_preslice_share"
    wire = json.dumps(table, ensure_ascii=False).casefold()
    assert not any(x in wire for x in ("select ", "ywzt_ods.", "traceback (", "password=")), "internal_disclosure"


def verify(table, rows, baseline, *, first=None, second=None):
    assert table.get("table_id") == "model_verified", "not_model_execution"
    # Concrete requested dates carry the same meaning as first/second day;
    # never accept a fuzzy substring, another date, or a swapped period.
    aliases = {}
    for day, label in ((first, LABELS[2]), (second, LABELS[3])):
        if day is not None:
            aliases.update({f"{day.month}月{day.day}日实发数量": label,
                            f"{day}实发数量": label,
                            f"{day.year}年{day.month}月{day.day}日实发数量": label})
    table = {**table, "columns": [{**column, "label": aliases.get(column["label"], column["label"])}
                                  for column in table["columns"]]}
    values = records(table, rows, LABELS)
    columns = {c["label"]: c for c in table["columns"]}
    assert all(columns[name]["kind"] == "text" for name in LABELS[:2]), "identity_type"
    assert all(columns[name]["kind"] == "number" and columns[name].get("unit") == "EA"
               for name in LABELS[2:]), "quantity_type_or_unit"
    actual = [(a, b, _number(c), _number(d), _number(e)) for a, b, c, d, e in values]
    assert actual == baseline, "source_filter_order_or_quantity_mismatch"
    wire = json.dumps(table, ensure_ascii=False).casefold()
    assert not any(x in wire for x in ("select ", "ywzt_ods.", "traceback (", "password=")), "internal_disclosure"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--first", type=date.fromisoformat, default=date(2026, 9, 9))
    parser.add_argument("--task-id")
    parser.add_argument("--case", choices=("growth", "above_average", "coverage"), default="growth")
    args = parser.parse_args()
    first, second = args.first, args.first + timedelta(days=1)
    cases = {
        'growth': ('model_growth_filter', expected, question),
        'above_average': ('model_owner_average_filter', expected_above_average, average_question),
        'coverage': ('model_owner_coverage_filter', expected_coverage, coverage_question),
    }
    case, oracle, make_question = cases[args.case]
    text = make_question(first, second)
    assert RequestPlanner().classify(text) is None, "holdout_has_become_fixed_route"
    if not args.live:
        print(json.dumps({"case": case, "state": "not_run", "fixed_route": False}))
        return
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    sql = ledger_sql(first, second)
    baseline = oracle(validate_doris_result(bridge.query(sql=sql, purpose="model holdout independent baseline")).rows, first, second)
    started = time.monotonic()
    task = client.request("/v1/tasks/" + args.task_id) if args.task_id else client.request(
        "/v1/tasks", {"conversation_id": session["conversation_id"], "question": text},
        session["csrf_token"], "model-holdout-" + uuid4().hex)
    print(json.dumps({"case": case, "task_id": task["task_id"], "state": task["state"]}), flush=True)
    while task["state"] in {"queued", "running"} and time.monotonic() - started < 600:
        time.sleep(2)
        task = client.request("/v1/tasks/" + task["task_id"])
    print(json.dumps({"case": case, "task_id": task["task_id"], "state": task["state"],
                      "http_status": task.get("http_status"), "elapsed_seconds": round(time.monotonic()-started, 2)}), flush=True)
    assert task["state"] == "completed" and task.get("http_status") == 200, "task_incomplete"
    assert task["result"].get("result_state") in {"live_success", "no_data"}, "no_live_result"
    table = task["result"].get("business_table")
    assert isinstance(table, dict), "missing_table"
    after = oracle(validate_doris_result(bridge.query(sql=sql, purpose="model holdout source stability")).rows, first, second)
    assert baseline == after, "source_changed"
    rows = complete_rows(client, table)
    if args.case == "growth":
        verify(table, rows, baseline, first=first, second=second)
    elif args.case == 'above_average':
        verify_above_average(table, rows, baseline)
    else:
        verify_coverage(table, rows, baseline)
    print(json.dumps({"case": case, "verification": "passed", "fixed_route": False}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
