"""Monthly DMS measures checked against complete reviewed order details."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_generalization import ChatClient, _label, _number
from tests.acceptance.check_partition_ranking_live import complete_rows, submit


QUESTION = (
    "按已发布口径，查询DMS 2026年1至8月积分兑换订单，以订单创建时间划分月份，按北京时间。"
    "排除逻辑删除，不增加付款、WMS出库或商品过滤。订单先按已发布的版本规则处理，再按订单号去重。"
    "返回月份、订单数、活跃客户数、数量占比（%）、累计订单数五列。"
    "活跃客户数按当月非空客户编码去重，客户名称相同不能合并不同编码，缺失客户的订单仍计入订单数。"
    "数量占比是当月订单数除以1至8月全部订单数，分母为零时显示NULL；累计订单数按月份升序累计。"
    "1至8月每月必须有一行，无订单月份的订单数、活跃客户数按0展示，不要漏月。"
    "月份列为文本，订单数和累计订单数单位为单，活跃客户数单位为个，占比单位为%，数值列均为数值类型。"
    "只返回业务表格，不展示SQL或对账明细。"
)
REFERENCE_QUESTION = "查询DMS 2026年1至8月积分兑换订单，按月份统计订单数，并返回客户名称、客户编码和订单号明细"
MONTHS = tuple(f"2026-{month:02}" for month in range(1, 9))


def expected_months(rows):
    counts, clients, seen = Counter(), defaultdict(set), set()
    for row in rows:
        order = row["sales_order_code"]
        assert isinstance(order, str) and order.strip() and order not in seen, "reference_duplicate_order"
        seen.add(order)
        instant = datetime.fromisoformat(row["created_time"])
        if instant.tzinfo is not None:
            instant = instant.astimezone(ZoneInfo("Asia/Shanghai"))
        month = instant.strftime("%Y-%m")
        assert month in MONTHS, "reference_outside_window"
        counts[month] += 1
        customer = row["customer_code"]
        assert customer is None or isinstance(customer, str), "reference_invalid_customer_code"
        if customer is not None and customer.strip():
            clients[month].add(customer.strip())
    total, cumulative, result = len(rows), 0, {}
    for month in MONTHS:
        cumulative += counts[month]
        result[month] = (counts[month], len(clients[month]),
                         Decimal(counts[month]) / total * 100 if total else None, cumulative)
    return result


def verify(client, task, reference):
    for value in (task, reference):
        assert value["state"] == "completed" and value["http_status"] == 200, "task_incomplete"
        assert value["result"].get("result_state") in {"live_success", "no_data"}, "not_live_data"
    reference_tables = reference["result"].get("business_tables")
    assert isinstance(reference_tables, list) and len(reference_tables) == 2, "missing_reviewed_reference"
    detail_keys = {"sales_order_code", "customer_code", "customer_name", "created_time"}
    details = [t for t in reference_tables if {c["key"] for c in t.get("columns", [])} == detail_keys]
    assert len(details) == 1, "missing_reviewed_detail_projection"
    detail = details[0]
    expected = expected_months(complete_rows(client, detail))
    table = task["result"]["business_table"]
    rows = complete_rows(client, table)
    columns = {_label(c["label"]): c for c in table["columns"]}
    names = ("月份", "订单数", "活跃客户数", "数量占比", "累计订单数")
    assert set(columns) == set(names), "missing_or_extra_measure"
    assert columns["月份"]["kind"] == "text", "month_type_changed"
    for name, unit in zip(names[1:], ("单", "个", "%", "单")):
        assert columns[name]["kind"] == "number" and columns[name].get("unit") == unit, "measure_type_or_unit_missing"
    assert [r[columns["月份"]["key"]] for r in rows] == list(MONTHS), "missing_or_reordered_month"
    for row, month in zip(rows, MONTHS):
        actual = [row[columns[name]["key"]] for name in names[1:]]
        expected_count, expected_customers, expected_share, expected_cumulative = expected[month]
        assert _number(actual[0]) == expected_count, "order_count_mismatch"
        assert _number(actual[1]) == expected_customers, "customer_count_mismatch"
        assert _number(actual[3]) == expected_cumulative, "cumulative_count_mismatch"
        if expected_share is None:
            assert actual[2] is None, "zero_denominator_not_null"
        else:
            assert abs(_number(actual[2]) - expected_share) <= Decimal("0.005"), "share_denominator_mismatch"
    wire = json.dumps(task["result"], ensure_ascii=False).casefold()
    assert not any(s in wire for s in ("select ", "dms_ods.", "password=", "result_columns")), "internal_details_exposed"
    return {"months": len(rows), "reference_orders": sum(item[0] for item in expected.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"case": "dms_monthly_analysis", "state": "not_run"}))
        return
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    started = time.monotonic()
    task = client.request("/v1/tasks/" + args.task_id) if args.task_id else submit(client, session, QUESTION)
    assert task["state"] == "completed" and task["http_status"] == 200, "task_incomplete"
    reference = submit(client, session, REFERENCE_QUESTION)
    counts = verify(client, task, reference)
    print(json.dumps({"case": "dms_monthly_analysis", "task_id": task["task_id"], "state": task["state"],
                      **counts, "elapsed_seconds": round(time.monotonic() - started, 2)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
