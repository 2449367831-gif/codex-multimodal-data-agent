from datetime import datetime

import pytest

from app.agent_core.planning import RequestPlanner
from app.agent_core.semantic_types import SemanticRuntimeError


def test_calendar_periods_use_shanghai_natural_boundaries():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("今年销售出库") == (datetime(2026, 1, 1).date(), datetime(2026, 9, 11).date())
    assert planner._source_interval("本季度销售出库") == (datetime(2026, 7, 1).date(), datetime(2026, 9, 11).date())


def test_previous_quarter_and_year_are_complete_half_open_periods():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("上季度销售出库") == (datetime(2026, 4, 1).date(), datetime(2026, 7, 1).date())
    assert planner._source_interval("去年销售出库") == (datetime(2025, 1, 1).date(), datetime(2026, 1, 1).date())


def test_recent_windows_accept_numeric_and_chinese_durations():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("最近14天销售出库") == (datetime(2026, 8, 28).date(), datetime(2026, 9, 11).date())
    assert planner._source_interval("最近十四天销售出库") == (datetime(2026, 8, 28).date(), datetime(2026, 9, 11).date())
    assert planner._source_interval("过去两天销售出库") == (datetime(2026, 9, 9).date(), datetime(2026, 9, 11).date())
    assert planner._source_interval("前7天销售出库") == (datetime(2026, 9, 4).date(), datetime(2026, 9, 11).date())


def test_week_boundaries_use_shanghai_natural_week():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("上周销售出库") == (datetime(2026, 8, 31).date(), datetime(2026, 9, 7).date())
    assert planner._source_interval("本周销售出库") == (datetime(2026, 9, 7).date(), datetime(2026, 9, 11).date())


def test_nested_previous_calendar_periods_do_not_match_the_nearest_period():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("上上周销售出库") == (datetime(2026, 8, 24).date(), datetime(2026, 8, 31).date())
    assert planner._source_interval("上上个月销售出库") == (datetime(2026, 7, 1).date(), datetime(2026, 8, 1).date())
    assert planner._source_interval("上上季度销售出库") == (datetime(2026, 1, 1).date(), datetime(2026, 4, 1).date())


def test_nested_previous_periods_cross_year_boundaries():
    planner = RequestPlanner(clock=lambda: datetime(2026, 2, 3, 12))
    assert planner._source_interval("上上个月销售出库") == (datetime(2025, 12, 1).date(), datetime(2026, 1, 1).date())
    assert planner._source_interval("上上季度销售出库") == (datetime(2025, 7, 1).date(), datetime(2025, 10, 1).date())


def test_numbered_quarter_and_multi_period_windows_are_explicit():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("2026年第2季度销售出库") == (datetime(2026, 4, 1).date(), datetime(2026, 7, 1).date())
    assert planner._source_interval("最近3周销售出库") == (datetime(2026, 8, 17).date(), datetime(2026, 9, 11).date())
    assert planner._source_interval("近2个月销售出库") == (datetime(2026, 7, 1).date(), datetime(2026, 9, 1).date())


def test_numbered_quarter_does_not_use_future_data():
    planner = RequestPlanner(clock=lambda: datetime(2026, 5, 1, 12))
    assert planner._source_interval("2026年第3季度销售出库") is None


@pytest.mark.parametrize("question", ["本月截至昨天销售出库", "本月截止昨日销售出库", "本月截至今天00:00销售出库"])
def test_calendar_period_cutoff_is_half_open(question):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval(question) == (datetime(2026, 9, 1).date(), datetime(2026, 9, 11).date())


def test_explicit_ranges_handle_inherited_year_and_midnight_end():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("2025年12月31日到1月2日销售出库") == (
        datetime(2025, 12, 31).date(), datetime(2026, 1, 3).date()
    )
    assert planner._source_interval("2025年12月31日到2026年1月2日凌晨销售出库") == (
        datetime(2025, 12, 31).date(), datetime(2026, 1, 2).date()
    )


@pytest.mark.parametrize("clock", ["0 点", "凌晨 0 点", "00 : 00", "00 ： 00 ： 00", "零 点"])
def test_whitespace_in_midnight_keeps_exclusive_end(clock):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval(f"2026-09-01 至 2026-09-10 {clock}前销售出库") == (
        datetime(2026, 9, 1).date(), datetime(2026, 9, 10).date()
    )


@pytest.mark.parametrize("clock", ["2 点", "凌晨 2 点", "00 : 30", "0 点 30 分"])
def test_spaced_non_midnight_is_not_expanded_to_whole_day(clock):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval(f"2026-09-01 至 2026-09-10 {clock}前销售出库") is None


def test_invalid_explicit_date_is_not_silently_reinterpreted():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    try:
        planner._source_interval("2026年2月30日销售出库")
    except SemanticRuntimeError as exc:
        assert exc.reason == "date_invalid"
    else:
        raise AssertionError("invalid date was accepted")


@pytest.mark.parametrize("question", [
    "2026年9月10日至2026年9月1日销售出库",
    "2024年12月31日至2月29日销售出库",
    "2026年13月1日销售出库",
    "2026-02-30销售出库",
])
def test_invalid_interval_does_not_fall_back_to_month(question):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    with pytest.raises(SemanticRuntimeError) as failure:
        planner._source_interval(question)
    assert failure.value.reason == "date_invalid"


def test_omitted_end_year_is_resolved_before_leap_day_validation():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval("2023年12月31日至2月29日销售出库") == (
        datetime(2023, 12, 31).date(), datetime(2024, 3, 1).date()
    )
    assert planner._source_interval("2024年2月28日至2月29日销售出库") == (
        datetime(2024, 2, 28).date(), datetime(2024, 3, 1).date()
    )


@pytest.mark.parametrize("question", [
    "2026年9月1日和2026年9月3日销售出库",
    "2026年9月1日到2026年9月3日凌晨2点销售出库",
    "2026-09-01 12:30至2026-09-03 00:00销售出库",
    "2026-09-01 12:30销售出库",
])
def test_unrepresented_time_scope_never_expands_to_a_day_or_month(question):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    assert planner._source_interval(question) is None
