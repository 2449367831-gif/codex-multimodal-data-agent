"""Equivalent syntax must preserve dates, grain, and literal scope."""

from datetime import date, datetime, timedelta
from itertools import permutations

import pytest

from app.agent_core.planning import RequestPlanner
from app.query_scope import ScopeResolver
from app.request_syntax import normalize_query_wording, normalize_recent_day_windows, recent_day_count


@pytest.fixture
def planner():
    return RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12))


@pytest.mark.parametrize("raw,days", [
    ("七", 7), ("十四", 14), ("14", 14), ("１４", 14), ("三十", 30), ("九十九", 99), ("两", 2),
])
@pytest.mark.parametrize("prefix", ["近", "最近", "过去", "前"])
@pytest.mark.parametrize("suffix", ["天", "个完整自然日", " 个 完整 自然 日"])
def test_duration_syntax_resolves_through_whole_planner(planner, raw, days, prefix, suffix):
    question = f"{prefix}{raw}{suffix}各仓库实际销售出库"
    normalized = normalize_recent_day_windows(question)
    assert normalized == f"近{days}天各仓库实际销售出库"
    assert normalize_recent_day_windows(normalized) == normalized
    assert ScopeResolver().resolve(question)[2] is False
    result = planner.classify(question)
    assert result is not None
    assert (result.start, result.end) == (date(2026, 9, 15) - timedelta(days=days), date(2026, 9, 15))
    assert result.scope.owner == "ALL" and not result.scope.sku_codes


@pytest.mark.parametrize("raw", ["0", "3651", "一一", "十十", "二十三四", "一百", "-14"])
def test_unsupported_number_does_not_become_a_different_window(raw):
    assert recent_day_count(raw) is None
    question = f"最近{raw}天实际销售出库"
    assert normalize_recent_day_windows(question) == question


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_quoted_duration_is_not_rewritten(quote):
    question = f"近十四天名称包含{quote}近七天{quote}的商品实际销售出库"
    assert normalize_recent_day_windows(question) == f"近14天名称包含{quote}近七天{quote}的商品实际销售出库"


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_quoted_week_duration_is_not_rewritten(quote):
    question = f"最近两周名称包含{quote}最近一周{quote}的商品实际销售出库"
    assert normalize_recent_day_windows(question) == f"近14天名称包含{quote}最近一周{quote}的商品实际销售出库"


@pytest.mark.parametrize("metric", ["净销售数量", "销售数量", "预售数量", "销售量"])
def test_command_normalization_does_not_redefine_sales_measures(metric):
    question = f"查询销售订单 S1 的退货数量和{metric}"
    assert normalize_query_wording(question) == question


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_colloquial_quantity_normalization_preserves_literals(quote):
    question = f"昨天卖出数量最高的20个SKU，名称包含{quote}卖出数量{quote}"
    normalized = normalize_query_wording(question)
    assert normalized == f"昨天实际销售出库量TOP20的SKU，名称包含{quote}卖出数量{quote}"
    assert normalize_query_wording(normalized) == normalized


def test_concatenated_owner_stays_separate_from_time_window(planner):
    request = planner.classify("近十四天HJXH实际销售出库，按仓库")
    assert request is not None and request.scope.owner == "HJXH"
    assert (request.start, request.end) == (date(2026, 9, 1), date(2026, 9, 15))


def test_conflicting_recent_windows_do_not_select_first(planner):
    assert planner._source_interval("最近十四天和最近七天实际销售出库") is None
    assert planner.classify("最近十四天和最近七天实际销售出库") is None
    assert planner._source_interval("最近0天和最近十四天实际销售出库") is None


@pytest.mark.parametrize("dimensions", list(permutations(["仓库", "SKU", "客户渠道"])))
@pytest.mark.parametrize("analysis", ["", "，计算数量占比，返回SKU数", "，计算日均量"])
def test_group_permutations_preserve_every_dimension(planner, dimensions, analysis):
    request = planner.classify("最近十四个完整自然日实际销售出库，按" + "和".join(dimensions) + "汇总" + analysis)
    assert request is not None
    assert request.route == ("sales_analysis" if analysis else "sales_aggregate")
    assert set(request.sales_aggregation.dimensions) == {"warehouse", "sku", "customer_channel"}
    assert (request.start, request.end) == (date(2026, 9, 1), date(2026, 9, 15))


def test_sku_count_is_not_sku_dimension(planner):
    request = planner.classify("近十四天实际销售出库，按仓库和客户渠道汇总，计算数量占比，返回SKU数")
    assert request is not None
    assert request.sales_aggregation.dimensions == ("warehouse", "customer_channel")


@pytest.mark.parametrize("code", ["123456", "2026-01-01", "AbC_2C-HJXH"])
def test_group_cleanup_does_not_erase_explicit_sku(planner, code):
    request = planner.classify(f"近十四天实际销售出库，SKU `{code}`，按客户渠道汇总")
    assert request is not None
    assert request.scope.sku_codes == (code,)
    assert request.scope.owner == "ALL"
    assert request.sales_aggregation.dimensions == ("customer_channel",)
    assert (request.start, request.end) == (date(2026, 9, 1), date(2026, 9, 15))


@pytest.mark.parametrize("extra", [
    "，渠道为抖音", "，排除客户甲", "，按平台统计", "，单位为箱", "，按订单创建时间",
    "，订单数>100", "，最近24小时", "，仅限武汉未知仓", "，仅含客户渠道编码123",
])
def test_unknown_predicates_survive_syntax_cleanup(planner, extra):
    assert planner.classify("最近十四个完整自然日实际销售出库，按客户渠道汇总" + extra) is None


def test_supported_quantity_predicate_survives_syntax_cleanup(planner):
    request = planner.classify("最近十四个完整自然日实际销售出库，按客户渠道汇总，数量>100")
    assert request is not None and request.route == "sales_analysis"
    assert request.sales_aggregation.dimensions == ("customer_channel",)
    assert len(request.sales_analysis.quantity_predicates) == 1
