"""Acceptance must reject a live but wrong-grain or incomplete answer."""

from copy import deepcopy

import pytest

from scripts.evaluate_generalization import ChatClient, check_answer, check_calculations


CASE = {"required_columns": [["类型"], ["仓库数"]], "forbidden_columns": ["仓库编码"], "max_rows": 20}


def completed():
    return {"state": "completed", "result": {"result_state": "live_success",
        "business_result": {"text": "测试结果"}, "business_table": {
            "columns": [{"label": "类型"}, {"label": "仓库数"}],
            "rows": [{"type": "synthetic", "count": "2"}]}}}


def test_checked_shape_passes():
    assert check_answer(CASE, completed()) == []


def test_successful_lookup_is_not_a_successful_grouping():
    task = completed()
    task["result"]["business_table"]["columns"] = [{"label": "类型"}, {"label": "仓库编码"}]
    assert check_answer(CASE, task) == ["missing_requested_column_2", "wrong_output_grain"]


@pytest.mark.parametrize("state", ["failed", "no_data", "clarification_required", "result_truncated"])
def test_terminal_does_not_imply_answered(state):
    task = completed()
    task["result"]["result_state"] = state
    assert check_answer(CASE, task) == ["no_complete_live_answer"]


def test_deadline_does_not_claim_failure_of_running_job():
    assert check_answer(CASE, {"state": "running"}) == ["no_complete_live_answer"]


def test_missing_table_extra_rows_and_internal_details_are_detected():
    task = completed()
    task["result"]["business_table"]["rows"] *= 21
    assert check_answer(CASE, task) == ["wrong_ranking_limit"]
    task = completed()
    task["result"]["business_result"]["text"] = "SELECT secret FROM ywzt_ods.example"
    assert check_answer(CASE, task) == ["internal_details_exposed"]


def test_generic_model_columns_are_projected_to_business_labels():
    from app.chat_api import _public_model_verified_table
    table = _public_model_verified_table({
        "columns": ["actual_ship_qty", "unknown_internal_alias"],
        "rows": [{"actual_ship_qty": 10, "unknown_internal_alias": "x"}],
    })
    assert [column["label"] for column in table["columns"]] == ["实发数量", "结果列 2"]
    assert [column["key"] for column in table["columns"]] == ["col_1", "col_2"]
    assert "actual_ship_qty" not in str(table)
    assert "unknown_internal_alias" not in str(table)


def test_internal_model_prose_is_replaced_without_losing_verified_table():
    from app.chat_api import _public_business_result
    public = _public_business_result(
        {"outcome": "completed", "answer": "查询 SELECT secret FROM ywzt_ods.example 字段"},
        live=True,
    )
    assert public == {"format": "plain_text", "text": "查询已完成，结果已按已校验表格展示。"}


@pytest.mark.parametrize("configured", [None, [False, True, False]])
def test_generic_model_table_hides_execution_evidence(configured):
    from app.chat_api import _public_model_verified_table
    table = _public_model_verified_table({
        "columns": ["validation_status", "actual_ship_qty", "source_max_ship_at"],
        "numeric_columns": configured,
        "rows": [{"validation_status": "ok", "actual_ship_qty": 10,
                  "source_max_ship_at": "2026-09-11 12:00:00"}],
    })
    assert [column["label"] for column in table["columns"]] == ["实发数量"]
    assert table["rows"] == [{"col_1": "10"}]
    assert table["columns"][0]["kind"] == "number"
    assert "2026-09-11" not in str(table)


def test_execution_evidence_alone_is_not_a_business_table():
    from app.chat_api import _public_model_verified_table
    assert _public_model_verified_table({
        "columns": ["validation_status"], "rows": [{"validation_status": "ok"}],
    }) is None


@pytest.mark.parametrize("url", ["https://example.org", "file:///private", "http://127.0.0.1.example.org"])
def test_live_runner_is_loopback_only(url):
    with pytest.raises(ValueError):
        ChatClient(url)


@pytest.mark.parametrize("quantities,failed", [([3, 2, 1], False), ([1, 3, 2], True), (["NaN"], True)])
def test_ranking_checks_values_not_just_table_shape(quantities, failed):
    case = {"required_columns": [["SKU"], ["数量"]], "checks": ["descending_quantity"]}
    table = {"columns": [{"label": "数量", "key": "q"}], "rows": [{"q": n} for n in quantities]}
    assert bool(check_calculations(case, table)) == failed


@pytest.mark.parametrize("count,failed", [(2, False), (0, True), ("1.5", True), (True, True)])
def test_counts_are_positive_integers(count, failed):
    case = {"required_columns": [["类型"], ["仓库数"]], "checks": ["positive_integer_count"]}
    table = {"columns": [{"label": "仓库数", "key": "n"}], "rows": [{"n": count}]}
    assert bool(check_calculations(case, table)) == failed


def test_share_cannot_be_an_unrelated_percentage():
    case = {"required_columns": [["类型"], ["数量"], ["单数"], ["占比"]], "checks": ["quantity_share"]}
    table = {"columns": [{"label": label, "key": key} for key, label in [("q", "数量"), ("n", "单数"), ("p", "占比")]],
             "rows": [{"q": 30, "n": 1, "p": "75%"}, {"q": 10, "n": 2, "p": "25%"}]}
    assert check_calculations(case, table) == []
    table["rows"][0]["p"] = "25%"
    assert check_calculations(case, table) == ["calculation_failed_quantity_share"]


@pytest.mark.parametrize("shares,failed", [(["75%", "25%"], False), (["50%", "50%"], True), (["75%", "15%"], True)])
def test_partition_shares_match_the_counts(shares, failed):
    case = {"required_columns": [["类型"], ["数量"], ["占比"]], "checks": ["partition_share"]}
    table = {"columns": [{"key": "n", "label": "数量"}, {"key": "p", "label": "占比"}],
        "rows": [{"n": 3, "p": shares[0]}, {"n": 1, "p": shares[1]}]}
    assert bool(check_calculations(case, table)) == failed


def test_partition_share_does_not_double_count_a_verified_total_row():
    case = {"required_columns": [["类型"], ["数量"], ["占比"]],
            "checks": ["positive_integer_count", "partition_share"]}
    table = {"columns": [{"key": "t", "label": "类型"}, {"key": "n", "label": "数量"},
                         {"key": "p", "label": "占比"}],
             "rows": [{"t": "合计", "n": 4, "p": "100%"},
                      {"t": "A", "n": 3, "p": "75%"}, {"t": "B", "n": 1, "p": "25%"}]}
    assert check_calculations(case, table) == []


@pytest.mark.parametrize("label", ["数量占比", "数量占比(%)", "数量占比（％）", "数量占比 (%)"])
def test_share_label_typography_does_not_hide_valid_data_or_skip_arithmetic(label):
    case = {"required_columns": [["仓库类型"], ["仓库数"], ["数量占比"]], "checks": ["partition_share"]}
    table = {"columns": [{"key": "t", "label": "仓库类型"}, {"key": "n", "label": "仓库数"}, {"key": "p", "label": label}],
             "rows": [{"t": "A", "n": 3, "p": "75.00"}, {"t": "B", "n": 1, "p": "25.00"}]}
    task = {"state": "completed", "result": {"result_state": "live_success", "business_table": table,
                                             "business_result": {"text": "合成结果"}}}
    assert check_answer(case, task) == []
    table["rows"][0]["p"] = "0.75"
    assert check_answer(case, task) == ["calculation_failed_partition_share"]


def test_label_matching_does_not_remove_business_units_or_substitute_money_for_quantity():
    task = completed()
    task["result"]["business_table"]["columns"] = [{"label": "数量(EA)"}, {"label": "金额占比"}]
    case = {"required_columns": [["数量(箱)"], ["数量占比"]]}
    assert check_answer(case, task) == ["missing_requested_column_1", "missing_requested_column_2"]


def test_correct_share_total_does_not_hide_duplicate_business_groups():
    case = {"required_columns": [["类型"], ["数量"], ["占比"]], "checks": ["partition_share", "unique_groups"]}
    table = {"columns": [{"key": "t", "label": "类型"}, {"key": "n", "label": "数量"}, {"key": "p", "label": "占比"}],
             "rows": [{"t": "A", "n": 3, "p": "75%"}, {"t": " A ", "n": 1, "p": "25%"}]}
    assert check_calculations(case, table) == ["calculation_failed_unique_groups"]
    table["rows"][1]["t"] = "B"
    assert check_calculations(case, table) == []


def test_unique_groups_respects_a_declared_compound_grain():
    case = {"required_columns": [["货主"], ["仓库"]], "checks": ["unique_groups"], "group_column_indices": [0, 1]}
    table = {"columns": [{"key": "o", "label": "货主"}, {"key": "w", "label": "仓库"}],
             "rows": [{"o": "A", "w": "1"}, {"o": "B", "w": "1"}]}
    assert check_calculations(case, table) == []
    table["rows"].append({"o": "A", "w": "1"})
    assert check_calculations(case, table) == ["calculation_failed_unique_groups"]


def test_period_averages_and_missing_data_are_independently_checked():
    case = {"checks": ["period_daily_average"], "period_days": [7, 6]}
    labels = ["期间一出库数量", "期间二出库数量", "期间一日均量", "期间二日均量", "日均变化率"]
    table = {"columns": [{"key": str(i), "label": label} for i, label in enumerate(labels)],
             "rows": [dict(zip(map(str, range(5)), values)) for values in [
                 [70, 120, 10, 20, "100%"], ["无记录", 60, "无记录", 10, "无记录"],
                 [0, 60, 0, 10, "分母为零"]]]}
    assert check_calculations(case, table) == []
    table["rows"][0]["3"] = "17.14"
    assert check_calculations(case, table) == ["calculation_failed_period_daily_average"]


def period_with_states():
    labels = ["期间一出库数量", "期间二出库数量", "期间一日均量", "期间二日均量", "日均变化率",
              "期间一记录状态", "期间二记录状态"]
    return {"columns": [{"key": str(i), "label": label} for i, label in enumerate(labels)],
            "rows": [dict(zip(map(str, range(7)), values)) for values in [
                [70, 120, 10, 20, "100%", "有记录", "有记录"],
                [None, 60, None, 10, "无记录", "无记录", "有记录"],
                [0, 60, 0, 10, "分母为零", "有记录", "有记录"]]]}


def test_nullable_period_values_must_agree_with_record_states():
    table = period_with_states()
    assert check_calculations({"checks": ["period_daily_average"], "period_days": [7, 6]}, table) == []


@pytest.mark.parametrize("row,key,value", [
    (1, "0", 0), (1, "2", 0), (1, "5", "有记录"), (1, "4", "0%"),
    (0, "5", "未知"), (0, "5", "无记录"), (0, "0", None), (0, "2", None),
    (2, "4", "0%"), (0, "3", 17.14),
])
def test_period_protocol_or_value_contradictions_fail(row, key, value):
    table = period_with_states()
    table["rows"][row][key] = value
    assert check_calculations({"checks": ["period_daily_average"], "period_days": [7, 6]}, table) == [
        "calculation_failed_period_daily_average"]


def test_partial_record_state_protocol_is_rejected():
    table = period_with_states()
    table["columns"].pop()
    assert check_calculations({"checks": ["period_daily_average"], "period_days": [7, 6]}, table) == [
        "calculation_failed_period_daily_average"]


def test_fulfillment_share_route_is_deterministic():
    from app.agent_core.planning import RequestPlanner
    from datetime import datetime
    from zoneinfo import ZoneInfo
    planner = RequestPlanner(clock=lambda _zone: datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(
        "查询2026年9月11日HJXH货主产成品实际销售出库，分别计算2C和2B的实发数量、出库单数，"
        "并计算各自实发数量占当天总量的百分比；未配置履约类型的单据单列，不能丢弃。"
    )
    assert request is not None
    assert request.route == "sales_analysis"
    assert request.scope.owner == "HJXH"
    assert request.finished_goods_only is True
