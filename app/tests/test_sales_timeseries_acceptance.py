"""An independent calendar oracle must reject plausible, incomplete answers."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import runpy

import pytest

from app.agent_core.planning import RequestPlanner

CHECKER = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tests/acceptance/check_sales_timeseries_live.py"))
START, END = date(2026, 8, 29), date(2026, 9, 5)


def fixture(empty=False):
    facts = [] if empty else [
        {"ship_date": "2026-08-29", "quantity": "6", "outbound_count": "2"},
        {"ship_date": "2026-08-31", "quantity": "3", "outbound_count": "1"},
        {"ship_date": "2026-09-02", "quantity": "0", "outbound_count": "1"},
        {"ship_date": "2026-09-04", "quantity": "12", "outbound_count": "3"},
    ]
    expected = CHECKER["expected_series"](facts, START, END, 3)
    columns = [{"key": "d", "label": "日期", "kind": "text"}]
    for key, label, unit in (("q", "实发数量", "EA"), ("n", "出库单数", "单"),
                             ("c", "累计实发数量", "EA"), ("a", "3日移动平均", "EA/日")):
        columns.append({"key": key, "label": label, "kind": "number", "unit": unit})
    rows = [{"d": d, **{key: str(value) if value is not None else None
                          for key, value in zip(("q", "n", "c", "a"), values)}}
            for d, values in expected.items()]
    task = {"state": "completed", "http_status": 200, "result": {"result_state": "live_success",
            "business_table": {"columns": columns, "rows": rows}}}
    reference = {"state": "completed", "http_status": 200, "result": {
        "result_state": "no_data" if empty else "live_success", "business_table": {
            "columns": [{"key": k} for k in ("ship_date", "quantity", "outbound_count")], "rows": facts}}}
    return task, reference


@pytest.mark.parametrize("empty", [False, True])
def test_calendar_completion_null_prefix_zero_days_and_independent_values(empty):
    task, reference = fixture(empty)
    assert CHECKER["verify"](None, task, reference, START, END, 3) == {"days": 7, "moving_window_days": 3}
    if not empty:
        rows = task["result"]["business_table"]["rows"]
        assert [r["a"] for r in rows] == [None, None, "3", "1", "1", "0", "4"]
        assert [r["c"] for r in rows] == ["6", "6", "9", "9", "9", "9", "21"]


@pytest.mark.parametrize("mutation", ["drop_zero", "reverse", "quantity", "count", "cumulative", "rolling",
    "partial_average", "wrong_unit", "text_measure", "duplicate_date", "outside_window", "leak"])
def test_incorrect_successful_tables_fail(mutation):
    task, reference = deepcopy(fixture())
    table = task["result"]["business_table"]
    if mutation == "drop_zero":
        table["rows"].pop(1)
    elif mutation == "reverse":
        table["rows"].reverse()
    elif mutation in {"quantity", "count", "cumulative", "rolling"}:
        table["rows"][-1][{"quantity": "q", "count": "n", "cumulative": "c", "rolling": "a"}[mutation]] = "99"
    elif mutation == "partial_average":
        table["rows"][0]["a"] = "6"
    elif mutation == "wrong_unit":
        table["columns"][-1]["unit"] = "箱"
    elif mutation == "text_measure":
        table["columns"][1]["kind"] = "text"
    elif mutation == "duplicate_date":
        reference["result"]["business_table"]["rows"].append(reference["result"]["business_table"]["rows"][0])
    elif mutation == "outside_window":
        reference["result"]["business_table"]["rows"][0]["ship_date"] = "2026-09-05"
    else:
        task["result"]["business_result"] = {"text": "SELECT hidden FROM ywzt_ods.private"}
    with pytest.raises(AssertionError):
        CHECKER["verify"](None, task, reference, START, END, 3)


@pytest.mark.parametrize("window", [3, 5, 7])
def test_timeseries_uses_shared_calendar_operations_reference_uses_reviewed_facts(window):
    planner = RequestPlanner()
    request = planner.classify(CHECKER["question"](START, END, window))
    assert request is not None and request.route == "sales_timeseries"
    assert request.sales_timeseries.moving_window_days == window
    request = planner.classify(CHECKER["reference_question"](START, END))
    assert request is not None and request.route == "sales_aggregate"
    assert request.sales_aggregation.dimensions == ("ship_date",)
    assert request.start == START and request.end == END
    assert request.scope.owner == "ALL" and request.scope.finished_goods


@pytest.mark.parametrize("measure", ["实发数量", "实发量", "实发数量和出库单数", "出库单数和实发量"])
def test_explicit_actual_measures_are_not_treated_as_scope(measure):
    request = RequestPlanner().classify(f"2026-08-29至2026-09-04成品实际销售出库，按日期汇总{measure}")
    assert request is not None and request.route == "sales_aggregate"
    assert request.sales_aggregation.dimensions == ("ship_date",)
    assert request.start == START and request.end == END
    assert request.scope.owner == "ALL" and request.scope.finished_goods


@pytest.mark.parametrize("extra", ["，只保留出库单数大于100的日期", "，累计实发数量", "，3日移动平均",
                                  "，按日期补齐无记录为0", "，排除客户甲"])
def test_additional_conditions_are_not_consumed_as_plain_measures(extra):
    request = RequestPlanner().classify("2026-08-29至2026-09-04成品实际销售出库，按日期汇总实发数量" + extra)
    assert request is None


def test_quantity_filter_is_retained_as_a_post_aggregation_operation():
    request = RequestPlanner().classify(
        "2026-08-29至2026-09-04成品实际销售出库，按日期汇总实发数量，只保留实发数量大于100的日期"
    )
    assert request is not None and request.route == "sales_analysis"
    assert request.sales_aggregation.dimensions == ("ship_date",)
    assert len(request.sales_analysis.quantity_predicates) == 1
