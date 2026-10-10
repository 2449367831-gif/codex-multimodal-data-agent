"""Model-only purchase composition checked against independent unit ledgers.

Results stay in memory. Only task IDs, states and assertion categories are printed.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.planning import RequestPlanner
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient, _number
from tests.acceptance.check_partition_ranking_live import complete_rows, records
from tests.acceptance.check_purchase_receipts_live import independent_controls


LABELS = ["源单位代码", "前段实收数量", "后段实收数量", "前段明细数", "后段明细数",
          "实收增量", "后段每条平均实收数量"]


def question(first, middle, end):
    return (
        "按已发布的生产日期采购入库口径比较两个区间，采购以入库类型10识别。"
        f"北京时间生产日期前段为[{first} 00:00:00,{middle} 00:00:00)，"
        f"后段为[{middle} 00:00:00,{end} 00:00:00)。"
        f"两段都只取实际入库时间早于{end} 00:00:00的记录，不加实际入库时间下限。"
        "全部货主、仓库、商品，不限定成品，不沿用前一问的任何范围。"
        "按源单位代码分别汇总原始实收数量和入库明细数，不换算，不跨单位合计。"
        "保留任一段有记录的全部源单位，一段无记录时该段数量与明细数为零；零数量明细仍计数。"
        "实收增量等于后段减前段，后段每条平均实收数量等于后段实收数量除以后段明细数，"
        "后段明细数为零时平均量为空，平均量保留两位小数。"
        "按源单位代码升序，完整输出源单位代码、前段实收数量、后段实收数量、前段明细数、"
        "后段明细数、实收增量、后段每条平均实收数量七列业务表格。"
        "源单位代码保留文本，数量单位由同一行源单位代码说明，不猜测EA或箱；"
        "明细数单位为个。不要展示查询语句、数据库字段或内部核对明细。"
    )


def expected(first_rows, second_rows):
    periods = []
    for rows in (first_rows, second_rows):
        units = {}
        for row in rows:
            unit = row["source_unit"]
            assert isinstance(unit, str) and unit and unit == unit.strip(), "invalid_source_unit"
            assert unit not in units, "duplicate_source_unit"
            assert _number(row["invalid_quantity_count"]) == 0, "invalid_source_quantity"
            assert _number(row["invalid_key_count"]) == 0, "invalid_source_line_key"
            count, total = _number(row["detail_count"]), _number(row["unit_total"])
            assert count > 0 and count == count.to_integral_value(), "invalid_source_count"
            assert total >= 0, "invalid_source_quantity"
            units[unit] = total, count
        periods.append(units)
    result = []
    for unit in sorted(periods[0].keys() | periods[1].keys()):
        a, na = periods[0].get(unit, (Decimal(0), Decimal(0)))
        b, nb = periods[1].get(unit, (Decimal(0), Decimal(0)))
        result.append((unit, a, b, na, nb, b-a, b/nb if nb else None))
    return result


def verify(table, rows, baseline):
    assert table.get("table_id") == "model_verified", "not_model_execution"
    output = records(table, rows, LABELS)
    columns = {column["label"]: column for column in table["columns"]}
    assert columns[LABELS[0]]["kind"] == "text", "source_unit_type"
    assert not columns[LABELS[0]].get("unit"), "source_unit_type"
    assert all(columns[label]["kind"] == "number" for label in LABELS[1:]), "measure_type"
    assert all(not columns[LABELS[i]].get("unit") for i in (1, 2, 5, 6)), "invented_quantity_unit"
    assert all(columns[LABELS[i]].get("unit") == "个" for i in (3, 4)), "count_unit"
    assert len(output) == len(baseline), "incorrect_unit_population"
    for actual, wanted in zip(output, baseline):
        assert actual[0] == wanted[0], "unit_identity_or_order"
        assert tuple(_number(value) for value in actual[1:6]) == wanted[1:6], "quantity_count_or_difference"
        assert (actual[6] is None if wanted[6] is None else
                abs(_number(actual[6]) - wanted[6]) <= Decimal("0.005")), "incorrect_unit_average"
    wire = json.dumps(table, ensure_ascii=False).casefold()
    assert not any(x in wire for x in ("select ", "ywzt_ods.", "traceback (", "password=")), "internal_disclosure"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--first", type=date.fromisoformat, default=date(2026, 9, 9))
    parser.add_argument("--task-id")
    args = parser.parse_args()
    first, middle, end = args.first, args.first + timedelta(days=1), args.first + timedelta(days=2)
    text = question(first, middle, end)
    assert RequestPlanner().classify(text) is None, "holdout_has_become_fixed_route"
    if not args.live:
        print(json.dumps({"case": "model_purchase_periods", "state": "not_run", "fixed_route": False}))
        return
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    def baseline():
        results = [validate_doris_result(bridge.query(
            sql=independent_controls(start, stop, arrival_before=end),
            purpose="independent purchase period ledger")).rows
            for start, stop in ((first, middle), (middle, end))]
        return expected(*results)

    before = baseline()
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    started = time.monotonic()
    task = client.request("/v1/tasks/" + args.task_id) if args.task_id else client.request(
        "/v1/tasks", {"question": text, "conversation_id": session["conversation_id"]},
        session["csrf_token"], "model-purchase-" + uuid4().hex)
    print(json.dumps({"case": "model_purchase_periods", "task_id": task["task_id"], "state": task["state"]}), flush=True)
    while task["state"] in {"queued", "running"} and time.monotonic() - started < 600:
        time.sleep(2)
        task = client.request("/v1/tasks/" + task["task_id"])
    result = task.get("result") or {}
    print(json.dumps({"case": "model_purchase_periods", "task_id": task["task_id"], "state": task["state"],
                      "http_status": task.get("http_status"), "error_code": (result.get("error") or {}).get("code"),
                      "elapsed_seconds": round(time.monotonic()-started, 2)}), flush=True)
    assert task["state"] == "completed" and task.get("http_status") == 200, "task_incomplete"
    assert result.get("result_state") in {"live_success", "no_data"}, "no_live_result"
    table = result.get("business_table")
    assert isinstance(table, dict), "missing_table"
    assert before == baseline(), "source_changed"
    verify(table, complete_rows(client, table), before)
    print(json.dumps({"case": "model_purchase_periods", "verification": "passed", "fixed_route": False}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
