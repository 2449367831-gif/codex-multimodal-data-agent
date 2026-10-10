from dataclasses import replace
from datetime import date, datetime
from decimal import localcontext
from types import SimpleNamespace

import pytest

from app.agent_core.handlers.period_comparison import WarehousePeriodComparisonHandler
from app.agent_core.handlers.sales_aggregate import SalesAggregateHandler
from app.agent_core.planning import RequestPlanner
from app.agent_core.semantic_types import SemanticAnswer, SemanticRuntimeError
from app.chat_api import _public_business_table


QUESTION = ("对比2026年9月1日至7日和9月8日至13日的HJXH成品实际销售出库，"
            "按仓库列出两段期间出库数量和日均量，只比较日均量的增减幅，"
            "分母为零时单独标注，不把无记录写成0。用表格展示。")


def plan(question=QUESTION):
    return RequestPlanner(clock=lambda: datetime(2026, 9, 14, 12)).classify(question)


def test_date_shorthand_and_request_local_scope():
    request = plan()
    assert request.route == "warehouse_period_comparison"
    assert request.comparison_periods == ((date(2026, 9, 1), date(2026, 9, 8)),
                                          (date(2026, 9, 8), date(2026, 9, 14)))
    assert request.scope.owner == "HJXH" and request.scope.finished_goods
    assert plan(QUESTION.replace("HJXH成品", "")).scope.owner == "ALL"
    assert not plan(QUESTION.replace("HJXH成品", "")).scope.finished_goods


def test_each_warehouse_wording_uses_the_same_reviewed_route():
    request = plan(QUESTION.replace("按仓库列出", "各仓库列出"))
    assert request.route == "warehouse_period_comparison"
    assert request.scope.owner == "HJXH"


@pytest.mark.parametrize("group", ["按仓库", "各仓库", "所有仓库", "每个仓库"])
@pytest.mark.parametrize("measure", ["实际销售出库量", "实际销售出库数量", "有效销售出库量"])
@pytest.mark.parametrize("periods", ["2026-09-01到2026-09-07和2026-09-08到2026-09-14",
                                    "2026年9月1日至7日和9月8日至14日"])
def test_business_variants_preserve_both_intervals_and_all_scope(group, measure, periods):
    request = plan(f"查询{periods}{group}日均{measure}对比，用表格展示。")
    assert request is not None and request.route == "warehouse_period_comparison"
    assert request.comparison_periods == ((date(2026, 9, 1), date(2026, 9, 8)),
                                          (date(2026, 9, 8), date(2026, 9, 15)))
    assert request.scope.owner == "ALL" and request.scope.warehouse_codes == ()
    assert not request.scope.finished_goods


def test_quoted_sku_is_not_a_comparison_operator_or_extra_date():
    sku = "2026-09-01"
    request = plan(QUESTION + f" SKU `{sku}`")
    assert request is not None and request.scope.sku_codes == (sku,)
    assert request.comparison_periods == plan().comparison_periods


@pytest.mark.parametrize("extra", ["每天12点到18点", "日均量大于1000", "排除退货", "出库量差额大于0"])
def test_comparison_does_not_discard_additional_operations(extra):
    assert plan(QUESTION + extra) is None


@pytest.mark.parametrize("dates,expected", [
    ("2026-08-25至2026-08-31和2026-09-01至2026-09-14", date(2026, 8, 25)),
    ("2025年12月25日至31日和2026年1月1日至7日", date(2025, 12, 25)),
])
def test_explicit_cross_month_and_cross_year(dates, expected):
    request = plan(QUESTION.replace("2026年9月1日至7日和9月8日至13日", dates))
    assert request is not None and request.comparison_periods[0][0] == expected


@pytest.mark.parametrize("extra", ["按金额排序", "按SKU汇总", "只查2C", "只查JYCK",
                                      "转成箱", "再给库存", "按货主汇总", "再给TOP20", "仅限客户甲"])
def test_unrepresented_requirements_are_not_dropped(extra):
    assert plan(QUESTION + extra) is None


@pytest.mark.parametrize("dates", ["2026年2月30日至31日和3月1日至7日",
                                    "2026年9月8日至1日和9月8日至13日"])
def test_invalid_dates_fail_before_execution(dates):
    with pytest.raises(SemanticRuntimeError):
        plan(QUESTION.replace("2026年9月1日至7日和9月8日至13日", dates))


def record(owner, warehouse, quantity):
    return {"cargo_owner": owner, "warehouse_code": warehouse, "warehouse_name": owner + warehouse,
            "quantity": str(quantity), "outbound_count": "1"}


def execute(monkeypatch, first, second):
    calls = []

    def query(handler, request):
        calls.append(request)
        return SemanticAnswer("verified", {"rows": (first, second)[len(calls) - 1]})

    monkeypatch.setattr(SalesAggregateHandler, "run", query)
    execution = SimpleNamespace(legacy_owner="HJXH")
    answer = WarehousePeriodComparisonHandler(execution, timezone_name="Asia/Shanghai").run(plan())
    assert len(calls) == 2
    assert all(request.scope == plan().scope and request.sales_aggregation.dimensions == ("warehouse",)
               for request in calls)
    return answer


def test_daily_average_uses_each_period_days_and_keeps_owner_keys(monkeypatch):
    answer = execute(monkeypatch, [record("A", "W1", 70), record("B", "W1", 140)],
                     [record("A", "W1", 120), record("B", "W1", 60)])
    rows = answer.business_table["rows"]
    assert len(rows) == 2
    assert rows[0]["period_1_daily_average"] == "10.00"
    assert rows[0]["period_2_daily_average"] == "20.00"
    assert rows[0]["daily_average_change_rate"] == "100.00%"
    assert rows[1]["daily_average_change_rate"] == "-50.00%"
    assert rows[0]["daily_average_difference"] == "10.00"
    assert rows[1]["daily_average_difference"] == "-10.00"
    public = _public_business_table(answer.business_table, live=True)
    assert public is not None and len(public["rows"]) == 2
    columns = {column["key"]: column for column in public["columns"]}
    assert columns["period_1_daily_average"]["unit"] == "EA/日"
    assert columns["period_1_daily_average"]["kind"] == "number"
    assert columns["daily_average_difference"]["signed"]


def test_each_period_preserves_finished_goods_scope(monkeypatch):
    seen = []

    def query(handler, request):
        seen.append(request.finished_goods_only)
        return SemanticAnswer("verified", {"rows": []})

    monkeypatch.setattr(SalesAggregateHandler, "run", query)
    WarehousePeriodComparisonHandler(SimpleNamespace(legacy_owner="HJXH"), timezone_name="Asia/Shanghai").run(plan())
    assert seen == [True, True]


def test_zero_and_missing_periods_are_distinct(monkeypatch):
    answer = execute(monkeypatch, [record("A", "W1", 0), record("A", "W2", 70)],
                     [record("A", "W1", 60), record("A", "W3", 30)])
    rows = {row["warehouse_code"]: row for row in answer.business_table["rows"]}
    assert rows["W1"]["daily_average_change_rate"] == "分母为零"
    assert rows["W2"]["period_2_quantity"] is None
    assert rows["W2"]["period_2_record_state"] == "无记录"
    assert rows["W3"]["period_1_quantity"] is None
    assert rows["W3"]["period_1_record_state"] == "无记录"
    assert rows["W1"]["period_1_quantity"] == "0"
    assert rows["W1"]["period_1_record_state"] == "有记录"
    assert rows["W2"]["period_2_daily_average"] is None
    assert rows["W2"]["daily_average_difference"] is None
    public = _public_business_table(answer.business_table, live=True)
    assert public is not None
    assert {row["warehouse_code"]: row for row in public["rows"]}["W2"]["period_2_daily_average"] is None


def test_public_comparison_matches_independent_composition_oracle(monkeypatch):
    from tests.acceptance.check_composed_operators_live import compare_periods

    answer = execute(monkeypatch, [record("A", "W1", 70), record("A", "W2", 0)],
                     [record("A", "W1", 30), record("B", "W2", 120)])
    public = _public_business_table(answer.business_table, live=True)
    reference = [dict(owner=owner, warehouse=warehouse, left_quantity=left, right_quantity=right,
                      left_records=int(left is not None), right_records=int(right is not None),
                      missing_quantity=0, negative_quantity=0, missing_key=0, expected_rows=3)
                 for owner, warehouse, left, right in [("A", "W1", 70, 30), ("A", "W2", 0, None), ("B", "W2", None, 120)]]
    compare_periods(public, public["rows"], reference, plan().comparison_periods)


def test_comparison_does_not_inherit_low_decimal_precision(monkeypatch):
    with localcontext() as context:
        context.prec = 6
        answer = execute(monkeypatch, [record("A", "W1", "7000000000000000000000000000007")],
                         [record("A", "W1", "6000000000000000000000000000012")])
    row = answer.business_table["rows"][0]
    assert row["period_1_daily_average"] == "1000000000000000000000000000001.00"
    assert row["daily_average_difference"] == "1.00"


def test_comparison_does_not_silently_overwrite_duplicate_groups(monkeypatch):
    with pytest.raises(SemanticRuntimeError, match="重复"):
        execute(monkeypatch, [record("A", "W1", 70), record("A", "W1", 80)], [])


def test_second_period_failure_does_not_return_partial_result(monkeypatch):
    calls = []

    def query(handler, request):
        calls.append(request)
        if len(calls) == 2:
            raise SemanticRuntimeError("failed", reason="query_failed")
        return SemanticAnswer("verified", {"rows": [record("A", "W1", 70)]})

    monkeypatch.setattr(SalesAggregateHandler, "run", query)
    with pytest.raises(SemanticRuntimeError):
        WarehousePeriodComparisonHandler(SimpleNamespace(legacy_owner="HJXH"), timezone_name="Asia/Shanghai").run(plan())


def test_empty_and_invalid_period_plans(monkeypatch):
    assert execute(monkeypatch, [], []).business_table["rows"] == []
    with pytest.raises(SemanticRuntimeError):
        WarehousePeriodComparisonHandler(SimpleNamespace(legacy_owner="HJXH"), timezone_name="Asia/Shanghai").run(
            replace(plan(), comparison_periods=None))
