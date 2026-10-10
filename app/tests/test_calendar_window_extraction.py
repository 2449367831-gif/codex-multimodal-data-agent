"""Only the selected time axis is consumed; identifiers and predicates survive."""

from datetime import date, datetime

import pytest

from app.agent_core.calendar_window import take_calendar_window
from app.agent_core.planning import RequestPlanner


def extract(text):
    parser = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))._source_interval
    return take_calendar_window(text, interval_parser=parser)


@pytest.mark.parametrize("clock", ["0 点", "凌晨 0 点", "00 : 00", "00 ： 00 ： 00", "零 点"])
def test_numeric_date_and_clock_separator_survives_extraction(clock):
    window = extract(f"2026-08-01 至 2026-09-01 {clock}前实际销售出库明细")
    assert (window.start, window.end) == (date(2026, 8, 1), date(2026, 9, 1))
    assert "实际销售出库明细" in window.remainder


@pytest.mark.parametrize("clock", ["2 点", "00 : 30", "0 点 30 分", "上午12点"])
def test_partial_day_end_is_never_reinterpreted_as_a_whole_day(clock):
    assert extract(f"2026-08-01 至 2026-09-01 {clock}前实际销售出库明细") is None


@pytest.mark.parametrize("expression", [
    "2026-08-01至2026-09-010点前",
    "2026-08-01至2026-09-0112:00",
    "2026-08-0112:00至2026-09-01",
])
def test_merged_numeric_date_clock_cannot_hide_an_endpoint(expression):
    assert extract(expression + "实际销售出库明细") is None
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    assert planner._source_interval(expression + "实际销售出库明细") is None


@pytest.mark.parametrize("text", ["", "实际销售出库明细", "近7天和近14天", "昨天、昨天", "2026-09-01和2026-09-03"])
def test_zero_or_multiple_time_expressions_are_not_selected_arbitrarily(text):
    assert extract(text) is None


def test_removing_window_cannot_create_a_new_owner_identifier():
    window = extract("HJX近14天H实际销售出库明细")
    assert "HJXH" not in window.remainder.replace(" ", "")
    assert RequestPlanner().scope_resolver.resolve(window.remainder)[2] is True


def test_quantity_and_dimension_constraints_remain_available_to_the_planner():
    window = extract("近14天HJXH实际销售出库明细，数量>10，排除客户甲")
    assert all(text in window.remainder for text in ("HJXH", "数量>10", "排除客户甲"))


def test_calendar_month_and_midnight_qualifier_are_consumed_as_one_expression():
    window = extract("本月截至今天00:00实际销售出库明细")
    assert (window.start, window.end) == (date(2026, 9, 1), date(2026, 9, 10))
    assert "截至" not in window.remainder


@pytest.mark.parametrize("text", ["近14天10114790实际销售出库明细", "10114790近14天实际销售出库明细",
                                  "近14天 10114790 实际销售出库明细", "上月10114790实际销售出库明细"])
def test_unit_terminated_calendar_words_can_be_adjacent_to_numeric_warehouse_codes(text):
    window = extract(text)
    assert window is not None and "10114790" in window.remainder
    request = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(text)
    assert request is not None and request.route == "sales_outbound_details"
    assert request.scope.warehouse_codes == ("10114790",)


@pytest.mark.parametrize("window", ["近14个完整自然日", "昨天", "2026年8月", "上个季度"])
@pytest.mark.parametrize("scope_first", [True, False])
def test_numeric_warehouse_adjacency_keeps_receipt_time_axes_separate(window, scope_first):
    separator = "," if window[0].isdigit() else ""
    scoped_window = "10114790" + separator + window if scope_first else window + "10114790"
    request = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(
        f"查询{scoped_window}生产日期的采购入库明细，但不包含今天完成入库的数据。")
    assert request is not None and request.route == "purchase_received_detail"
    assert request.scope.warehouse_codes == ("10114790",)
    assert request.receipt_plan.arrival_before == date(2026, 9, 10)


@pytest.mark.parametrize("expression", ["12026年8月", "101147902026年8月", "2026-09-011", "近114000天"])
def test_numeric_boundaries_cannot_extract_part_of_an_invalid_number(expression):
    assert extract(expression) is None
