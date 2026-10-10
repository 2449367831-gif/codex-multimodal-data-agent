"""Arithmetic composition must preserve every scope and fact-set control."""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from itertools import product
from types import SimpleNamespace

import pytest
import sqlglot

from app.agent_core.handlers.sales_analysis import SalesAnalysisHandler
from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.sales_analysis import SalesAnalysis
from app.agent_core.semantic_types import SemanticRuntimeError
from app.chat_api import _public_business_table
from app.tests.test_sales_aggregation import facts, candidate_scope_rules


def planner():
    return RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))


@pytest.mark.parametrize("window,group,operation", list(product(
    ["昨天", "近14个完整自然日", "2026-08-25至2026-09-09", "上月"],
    ["各仓库", "按SKU", "按货主", "按日期", "按出库状态", "按出库类型", "按2C和2B"],
    ["计算数量占比", "计算区间日均量", "按实发数量降序", "计算数量占比，计算日均量，按数量升序"],
)))
def test_compositions_cover_windows_and_dimensions(window, group, operation):
    request = planner().classify(f"查询{window}实际销售出库，{group}，{operation}")
    assert request and request.route == "sales_analysis"
    assert request.scope.owner == "ALL"
    assert not request.scope.finished_goods
    assert request.sales_analysis.daily_average == ("日均" in operation)
    assert request.sales_analysis.quantity_share == ("占比" in operation)


def test_request_scope_and_opaque_codes_survive_operation_parsing():
    first = planner().classify("昨天HJXH成品实际销售出库，按仓库，SKU `2C-2026-01-01`，计算数量占比")
    assert first.scope.owner == "HJXH" and first.scope.finished_goods
    assert first.scope.sku_codes == ("2C-2026-01-01",)
    assert first.start == date(2026, 9, 9)
    assert first.sales_aggregation.dimensions == ("warehouse",)
    second = planner().classify("昨天实际销售出库，按仓库，计算数量占比")
    assert second.scope.owner == "ALL" and not second.scope.sku_codes and not second.scope.finished_goods


@pytest.mark.parametrize("group,dimensions", [
    ("货主和状态", {"cargo_owner", "shipment_status"}),
    ("状态和货主", {"cargo_owner", "shipment_status"}),
    ("SKU、货主和出库类型", {"sku", "cargo_owner", "ref_order_type"}),
    ("日期和仓库与履约类型", {"ship_date", "warehouse", "fulfillment_type"}),
    ("货主和仓库和出库状态和SKU", {"cargo_owner", "warehouse", "shipment_status", "sku"}),
])
def test_coordinated_dimensions_never_drop_a_group(group, dimensions):
    request = planner().classify(f"昨天实际销售出库，按{group}汇总，数量占比")
    assert request and request.route == "sales_analysis"
    assert set(request.sales_aggregation.dimensions) == dimensions


def test_new_date_syntax_works_with_active_release_vocabulary(monkeypatch):
    from app.knowledge_activation import active_snapshot
    monkeypatch.setattr("app.query_scope.active_snapshot", active_snapshot)
    request = planner().classify("2026-09-01至2026年9月10日零点前各仓库实际销售出库，日均量")
    assert request and (request.start, request.end) == (date(2026, 9, 1), date(2026, 9, 10))


@pytest.mark.parametrize("extra", [
    "排除客户甲", "仅限货主B", "按金额排序", "数量>10", "再给库存", "按订单创建时间",
    "实发数量占各货主总量的百分比", "数量占公司总量的百分比", "并按渠道拆分", "计算单数占比",
    "只保留2C", "状态为700", "单位改成箱", "给我环比", "再算中位数", "按数量升序，按数量降序",
])
def test_unrepresented_requirements_never_disappear(extra):
    assert planner().classify("昨天实际销售出库，按仓库，计算数量占比，" + extra) is None


@pytest.mark.parametrize("question", [
    "昨天实际销售出库，按仓库汇总，日均数量大于10",
    "昨天实际销售出库，按仓库汇总，出库单数量大于10",
    "昨天实际销售出库，按仓库汇总，只保留出库单数量大于10",
    "昨天实际销售出库，按仓库汇总，只保留日均数量大于10",
    "昨天实际销售出库，按仓库汇总，只保留分配数量大于10",
    "昨天实际销售出库，按仓库汇总，数量大于1000个",
    "昨天实际销售出库，按仓库汇总，数量大于1000 个",
    "昨天实际销售出库，按仓库汇总，数量大于1e3",
    "昨天实际销售出库，按仓库汇总，数量大于10001.00001",
    "昨天实际销售出库，按仓库汇总，数量大于8，或者数量小于2",
])
def test_quantity_filter_does_not_reinterpret_neighboring_or_invalid_measures(question):
    request = planner().classify(question)
    assert request is None or request.route != "sales_analysis"


@pytest.mark.parametrize("comparison,expected", [
    ("大于7", {"W1"}),
    ("大于等于8", {"W1"}),
    ("等于0", {"W2"}),
])
def test_quantity_filter_is_applied_after_complete_group_aggregation(facts, comparison, expected):
    _, public = run(facts, f"昨天HJXH实际销售出库，按仓库汇总，汇总后只保留数量{comparison}")
    assert public is not None
    assert {row["warehouse_code"] for row in public["rows"]} == expected
    # W1 is 1.25 + 2.50 + 4.25. Filtering detail rows first would not
    # produce the same result for the >7 case.
    if expected == {"W1"}:
        assert public["rows"][0]["quantity"] == "8"


def test_quantity_filter_empty_result_is_not_reported_as_source_no_data(facts):
    result, public = run(facts, "昨天实际销售出库，按仓库汇总，汇总后只保留数量大于100")
    assert public is not None
    assert public["rows"] == []
    assert public["empty_label"] == "查询成功，汇总后没有符合数量条件的分组"
    assert result["query_execution"]["result_state"] == "live_success"


def test_multiple_quantity_conditions_keep_scope_sorting_and_daily_average(facts):
    result, public = run(facts, "2026-09-01至2026-09-09实际销售出库，按仓库汇总，"
                         "只保留数量大于等于10EA且数量小于1009，按数量降序，计算日均量")
    assert result["query_plan"]["scope"]["owner"] == "ALL"
    assert [row["quantity"] for row in public["rows"]] == ["1008", "10"]
    assert [Decimal(row["daily_average"]) for row in public["rows"]] == [Decimal("112"), Decimal("1.11")]
    assert len(public["summary"]) == 1
    assert public["summary"][0]["value"] == "1018"


def test_invalid_fact_control_cannot_be_hidden_by_quantity_filter(facts):
    def corrupt(rows):
        rows[0]["total_quantity"] += 1
    with pytest.raises(SemanticRuntimeError):
        run(facts, "昨天实际销售出库，按仓库汇总，数量大于100", transform=corrupt)


def test_mixed_or_unrepresented_share_denominators_are_not_guessed():
    assert planner().classify("近14天实际销售出库，按仓库，数量占当天总量的百分比") is None
    assert planner().classify("近14天实际销售出库，按日期，数量占当天总量的百分比，数量占比") is None
    request = planner().classify("近14天实际销售出库，按日期和仓库，数量占当天总量的百分比")
    assert request.sales_analysis.share_by_day


@pytest.mark.parametrize("ending", ["2026-09-10 0 点前", "2026-09-10 00:00", "2026年9月10日零点前"])
def test_exclusive_midnight_survives_analysis_normalization(ending):
    question = f"统计2026-09-01至{ending}各仓库实际销售出库，计算日均量"
    request = planner().classify(question)
    assert request and request.route == "sales_analysis"
    assert (request.start, request.end) == (date(2026, 9, 1), date(2026, 9, 10))


def test_unrepresented_subday_window_cannot_shrink_to_one_day():
    assert planner().classify("2026-09-01至2026-09-10 12:00实际销售出库，按仓库，日均量") is None


def run(facts, question, transform=None):
    calls = []

    def query(*, sql, purpose):
        calls.append(sql)
        translated = sqlglot.transpile(sql, read="doris", write="sqlite")[0]
        rows = [dict(row) for row in facts.execute(translated)]
        if transform:
            transform(rows)
        return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
            "rows": rows, "row_count": len(rows), "truncated": False}}

    runtime = SemanticQueryRuntime(query_client=SimpleNamespace(query=query), clock=lambda: datetime(2026, 9, 10, 12))
    result = runtime.run_if_supported(question)
    assert result is not None
    assert result["query_execution"]["query_call_count"] == 1
    assert result["query_execution"]["live_business_result_executed"]
    assert all("ship_qty" in sql and "allocate_qty" not in sql for sql in calls)
    return result, _public_business_table(result["business_table"], live=True)


def test_actual_sql_preserves_zero_and_unconfigured_fulfillment(facts):
    result, public = run(facts, "昨天实际销售出库，分别计算2C和2B的实发数量、出库单数，并计算各自实发数量占当天总量的百分比；未配置履约类型的单据单列，不能丢弃。")
    assert public is not None
    rows = {row["fulfillment_type"]: row for row in public["rows"]}
    assert set(rows) == {"2C", "2B", "未配置"}
    assert rows["2C"]["quantity"] == "18"
    assert rows["2C"]["outbound_count"] == "2"
    assert rows["2C"]["quantity_share"] == "100.00%"
    assert rows["未配置"]["outbound_count"] == "1"
    assert rows["未配置"]["quantity_share"] == "0.00%"
    assert result["query_plan"]["analysis"]["quantity_share"]
    assert any("同一查询范围" in note for note in public["notes"])


def test_daily_average_includes_days_without_sales_and_sorts_exactly(facts):
    _, public = run(facts, "2026-09-01至2026-09-09实际销售出库，按仓库，计算数量占比，计算日均量，按数量降序")
    assert public is not None
    rows = public["rows"]
    assert list(map(lambda row: Decimal(row["quantity"]), rows)) == [Decimal("1008"), Decimal("10"), Decimal(0)]
    assert Decimal(rows[0]["daily_average"]) == Decimal("112.00")
    assert rows[0]["quantity_share"] == "99.02%"


def test_daily_share_denominator_is_day_not_whole_period(facts):
    _, public = run(facts, "2026-09-08至2026-09-09实际销售出库，按日期和仓库，数量占当天总量的百分比，日均量")
    rows = public["rows"]
    previous = [row for row in rows if row["ship_date"] == "2026-09-08"]
    assert previous[0]["quantity_share"] == "100.00%"
    assert Decimal(previous[0]["daily_average"]) == Decimal("1000.00")
    today = [row for row in rows if row["ship_date"] == "2026-09-09"]
    assert sum(Decimal(row["quantity_share"].removesuffix("%")) for row in today) == 100


def test_zero_denominator_and_no_data_are_distinct(facts):
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=0")
    result, public = run(facts, "昨天实际销售出库，按2C和2B，数量占比")
    assert all(row["quantity_share"] == "分母为零" for row in public["rows"])
    assert result["query_execution"]["result_state"] == "live_success"
    facts.execute("DELETE FROM ywzt_ods.scm_sales_shipment_detail")
    result, public = run(facts, "昨天实际销售出库，按2C和2B，数量占比")
    assert public["rows"] == []
    assert result["query_execution"]["result_state"] == "no_data"


def test_bad_fact_controls_block_arithmetic_and_publication(facts):
    def corrupt(rows):
        rows[0]["total_quantity"] += 1
    with pytest.raises(SemanticRuntimeError):
        run(facts, "昨天实际销售出库，按仓库，日均量", transform=corrupt)


def test_conflicting_header_cannot_be_hidden_by_share_calculation(facts):
    facts.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,outbound_code,facility_code,ship_at,status,ref_order_type,ref_order_code,'99',delete_flag,_fdl_marked_deleted FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O1'")
    with pytest.raises(SemanticRuntimeError):
        run(facts, "昨天实际销售出库，按2C和2B，数量占比")


def test_invalid_operations_fail_before_query():
    with pytest.raises(ValueError):
        SalesAnalysis(quantity_order="sql")
    with pytest.raises(ValueError):
        SalesAnalysis()
    request = planner().classify("昨天实际销售出库，按仓库，数量占比")
    handler = SalesAnalysisHandler(SimpleNamespace(legacy_owner="HJXH"), timezone_name="Asia/Shanghai")
    with pytest.raises(SemanticRuntimeError):
        handler.run(replace(request, sales_analysis=None))
