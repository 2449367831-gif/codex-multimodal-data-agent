"""Verify generic calendar/window analysis against reviewed daily facts in memory."""

import argparse
from datetime import date, datetime, timedelta
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


def question(start, end, window):
    return (
        f"按已发布的成品实际销售出库口径，查询北京时间{start} 00:00至{end} 00:00前，"
        "全部货主、全部仓库、全部SKU和全部渠道，不继承之前的问题范围。"
        "按每天汇总实发数量和出库单数，按日期升序；出库单按货主和出库单号联合去重。"
        "补齐区间内所有自然日，无匹配出库的日期实发数量和出库单数均为0。"
        f"计算区间起点到当日的累计实发数量，以及包含当日的连续{window}个自然日移动平均；"
        f"必须先补齐日期再算移动平均，每个完整窗口的分母固定为{window}，"
        f"区间开头不足{window}天的移动平均显示NULL，不引入区间之前的数据。"
        f"只返回日期、实发数量、出库单数、累计实发数量、{window}日移动平均五列业务表格。"
        "日期使用YYYY-MM-DD文本，数量和累计数量单位EA，出库单数单位单，移动平均单位EA/日，"
        "数值列必须为数值类型。不要展示SQL或对账明细。"
    )


def reference_question(start, end):
    return f"统计{start.isoformat()}至{(end - timedelta(days=1)).isoformat()}全部货主成品实际销售出库，按日期汇总实发数量"


def days(start, end):
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days)]


def expected_series(rows, start, end, window):
    calendar = days(start, end)
    assert 1 <= window <= len(calendar), "invalid_window"
    quantities = {day: Decimal(0) for day in calendar}
    documents = {day: Decimal(0) for day in calendar}
    seen = set()
    for row in rows:
        day = row["ship_date"]
        assert day in quantities, "reference_outside_window"
        identity = (row.get("cargo_owner"), day)
        assert identity not in seen, "reference_duplicate_day"
        seen.add(identity)
        quantity, count = _number(row["quantity"]), _number(row["outbound_count"])
        assert count >= 0 and count == count.to_integral_value(), "invalid_reference_count"
        quantities[day] += quantity
        documents[day] += count
    total, result = Decimal(0), {}
    for index, day in enumerate(calendar):
        total += quantities[day]
        average = (sum(quantities[d] for d in calendar[index-window+1:index+1]) / window
                   if index >= window - 1 else None)
        result[day] = (quantities[day], documents[day], total, average)
    return result


def verify(client, task, reference, start, end, window):
    for value in (task, reference):
        assert value["state"] == "completed" and value["http_status"] == 200, "task_incomplete"
        assert value["result"].get("result_state") in {"live_success", "no_data"}, "not_live_data"
    reference_table = reference["result"].get("business_table")
    assert isinstance(reference_table, dict), "missing_reviewed_reference"
    assert {"ship_date", "quantity", "outbound_count"} <= {c["key"] for c in reference_table["columns"]}, "wrong_reference_grain"
    expected = expected_series(complete_rows(client, reference_table), start, end, window)
    table = task["result"].get("business_table")
    assert isinstance(table, dict), "missing_business_table"
    rows = complete_rows(client, table)
    columns = {_label(c["label"]): c for c in table["columns"]}
    labels = ("日期", "实发数量", "出库单数", "累计实发数量", f"{window}日移动平均")
    assert len(columns) == len(table["columns"]) and set(columns) == set(labels), "missing_or_extra_measure"
    assert columns["日期"]["kind"] == "text", "date_type_changed"
    for name, unit in zip(labels[1:], ("EA", "单", "EA", "EA/日")):
        assert columns[name]["kind"] == "number" and columns[name].get("unit") == unit, "measure_type_or_unit_missing"
    assert [r[columns["日期"]["key"]] for r in rows] == days(start, end), "missing_or_reordered_day"
    for row, day in zip(rows, expected):
        for index, name in enumerate(labels[1:]):
            actual, value = row[columns[name]["key"]], expected[day][index]
            if value is None:
                assert actual is None, "partial_window_not_null"
            else:
                tolerance = Decimal(0) if index == 1 else Decimal("0.005")
                check = ("daily_quantity_mismatch", "daily_document_count_mismatch", "cumulative_mismatch", "moving_average_mismatch")[index]
                assert actual is not None and abs(_number(actual) - value) <= tolerance, check
    wire = json.dumps(task["result"], ensure_ascii=False).casefold()
    assert not any(x in wire for x in ("select ", "ywzt_ods.", "password=", "result_columns")), "internal_details_exposed"
    return {"days": len(rows), "moving_window_days": window}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--days", type=int, choices=range(7, 32), default=14)
    parser.add_argument("--window", type=int, choices=range(2, 15), default=3)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--task-id")
    args = parser.parse_args()
    if args.window > args.days:
        parser.error("window exceeds the calendar")
    if not args.live:
        print(json.dumps({"case": "sales_timeseries", "state": "not_run"}))
        return
    end = args.end or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = end - timedelta(days=args.days)
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    started = time.monotonic()
    reference_before = submit(client, session, reference_question(start, end))
    assert reference_before["state"] == "completed" and reference_before.get("http_status") == 200, "reference_incomplete"
    reference_rows = complete_rows(client, reference_before["result"]["business_table"])
    baseline = expected_series(reference_rows, start, end, args.window)
    task = client.request("/v1/tasks/" + args.task_id) if args.task_id else submit(client, session, question(start, end, args.window))
    print(json.dumps({"case": "sales_timeseries", "task_id": task["task_id"], "state": task["state"]}), flush=True)
    assert task["state"] == "completed" and task.get("http_status") == 200, "task_incomplete"
    assert task["result"].get("result_state") in {"live_success", "no_data"}, "not_live_data"
    reference = submit(client, session, reference_question(start, end))
    assert reference["state"] == "completed" and reference.get("http_status") == 200, "reference_incomplete"
    current = expected_series(complete_rows(client, reference["result"]["business_table"]), start, end, args.window)
    assert baseline == current, "source_changed_between_reads"
    evidence = verify(client, task, reference, start, end, args.window)
    print(json.dumps({"case": "sales_timeseries", "task_id": task["task_id"], "verification": "passed",
                      **evidence, "elapsed_seconds": round(time.monotonic() - started, 2)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
