"""Check calendar semantics independently before exercising real warehouse data."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
import sqlglot

from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.sales_timeseries import SalesTimeSeries, SalesTimeSeriesHandler, calendar_rows
from app.agent_core.semantic_types import SemanticRuntimeError
from app.chat_api import _public_business_table
from app.query_scope import apply_scope
from app.tests.test_sales_aggregation import facts
from tests.acceptance.check_sales_timeseries_live import question


START, END = date(2026, 9, 8), date(2026, 9, 15)


@pytest.mark.parametrize("length,window", [(7, 2), (14, 5), (31, 9), (5, 7), (366, 365)])
def test_parameters_and_explicit_half_open_interval(length, window):
    end = START + timedelta(days=length)
    request = RequestPlanner().classify(question(START, end, window))
    assert request is not None and request.route == "sales_timeseries"
    assert (request.start, request.end) == (START, end)
    assert request.sales_timeseries == SalesTimeSeries(window)
    assert request.sales_aggregation.dimensions == ("ship_date",)
    assert request.scope.finished_goods and request.scope.owner == "ALL"
    assert request.scope.warehouse_codes == request.scope.sku_codes == ()


@pytest.mark.parametrize("extra", [
    "排除客户甲", "改成按仓库和日期汇总", "按日期降序", "仅查询北京仓库", "单据金额大于100",
    "实发数量大于100", "只算线上渠道", "忽略逻辑删除", "移动平均取有销售天数作分母",
    "补齐后删除零数量日期", "累计从上年开始", "不要累计实发数量", "SKU `A123`",
    "仅限HJXH货主", "只查长沙黄兴仓库", "移动平均单位箱",
])
def test_unknown_or_conflicting_semantics_never_disappear(extra):
    assert RequestPlanner().classify(question(START, END, 3) + extra) is None


@pytest.mark.parametrize("old,new", [
    ("分母固定为3", "分母固定为2"), ("不足3天", "不足2天"), ("均为0", "均为NULL"),
    ("显示NULL", "显示0"), ("补齐区间内所有自然日", ""),
    ("实发数量、出库单数、累计实发数量", "实发数量、SKU数、累计实发数量"),
    ("连续3个自然日", "连续4个自然日"), ("按每天汇总", "按仓库和日期汇总"),
    ("单位EA", "单位箱"), ("不引入区间之前的数据", "引入区间之前的数据"),
])
def test_operation_mutations_do_not_reuse_the_original_plan(old, new):
    assert RequestPlanner().classify(question(START, END, 3).replace(old, new)) is None


def test_current_request_scope_and_type_are_preserved():
    scoped = question(START, END, 3).replace(
        "全部货主、全部仓库、全部SKU和全部渠道", "HJXH货主，长沙黄兴仓库，SKU `AbC123`，出库类型JYCK"
    )
    planner = RequestPlanner()
    request = planner.classify(scoped)
    assert request is not None and request.route == "sales_timeseries"
    assert request.scope.owner == "HJXH" and request.scope.warehouse_codes == ("10114790",)
    assert request.scope.sku_codes == ("AbC123",)
    assert request.sales_aggregation.order_types == ("JYCK",)
    following = planner.classify(question(START, END, 3))
    assert following.scope.owner == "ALL" and not following.scope.sku_codes


def test_zero_days_zero_quantity_documents_and_partial_windows():
    rows = calendar_rows([
        {"ship_date": "2026-09-09", "quantity": "12.75", "outbound_count": "3"},
        {"ship_date": "2026-09-11", "quantity": "0", "outbound_count": "2"},
        {"ship_date": "2026-09-12", "quantity": "2.25", "outbound_count": "1"},
    ], START, END, SalesTimeSeries(3))
    assert [row["quantity"] for row in rows] == ["0", "12.75", "0", "0", "2.25", "0", "0"]
    assert [row["outbound_count"] for row in rows] == ["0", "3", "0", "2", "1", "0", "0"]
    assert [row["cumulative_quantity"] for row in rows] == ["0", "12.75", "12.75", "12.75", "15.00", "15.00", "15.00"]
    assert [row["moving_average"] for row in rows] == [None, None, "4.25", "4.25", "0.75", "0.75", "0.75"]


def test_empty_calendar_is_explicit_zero_and_long_window_remains_null():
    rows = calendar_rows([], START, END, SalesTimeSeries(9))
    assert len(rows) == 7 and all(row["quantity"] == row["outbound_count"] == "0" for row in rows)
    assert all(row["moving_average"] is None for row in rows)


def test_decimal_precision_is_isolated():
    with localcontext() as context:
        context.prec = 3
        rows = calendar_rows([{"ship_date": START.isoformat(), "quantity": "1000000000000000000000000000001.01",
                               "outbound_count": "1"}], START, START + timedelta(days=3), SalesTimeSeries(3))
        assert rows[-1]["cumulative_quantity"] == "1000000000000000000000000000001.01"
        assert rows[-1]["moving_average"] == "333333333333333333333333333333.67"
        assert context.prec == 3


@pytest.mark.parametrize("field,value", [("ship_date", "2026-09-15"), ("ship_date", "20260909"),
    ("ship_date", "2026-09-07"), ("ship_date", None), ("quantity", None), ("quantity", "-1"),
    ("outbound_count", "0"), ("outbound_count", "1.2"), ("quantity", "NaN")])
def test_bad_facts_are_not_zero_filled(field, value):
    row = {"ship_date": "2026-09-09", "quantity": "1", "outbound_count": "1", field: value}
    with pytest.raises((ValueError, TypeError)):
        calendar_rows([row], START, END, SalesTimeSeries(3))


def test_duplicate_date_is_not_overwritten():
    row = {"ship_date": "2026-09-09", "quantity": "1", "outbound_count": "1"}
    with pytest.raises(ValueError):
        calendar_rows([row, row], START, END, SalesTimeSeries(3))


@pytest.mark.parametrize("value", [0, 1, 366, True, "5", 5.0])
def test_window_budget_is_typed(value):
    with pytest.raises(ValueError):
        SalesTimeSeries(value)


def test_handler_uses_governed_facts_and_preserves_current_filters(facts):
    request = RequestPlanner().classify(question(START, END, 3))
    queries = []

    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            queries.append(sql)
            sql = apply_scope(sql, request.scope, legacy_owner="HJXH")
            return [dict(row) for row in facts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]

    answer = SalesTimeSeriesHandler(Execution(), timezone_name="Asia/Shanghai").run(request)
    table = _public_business_table(answer.business_table, live=True)
    assert table is not None
    assert [column["label"] for column in table["columns"]] == ["日期", "实发数量", "出库单数", "累计实发数量", "3日移动平均"]
    assert [row["quantity"] for row in table["rows"]][:4] == ["1000", "13.75", "100", "0"]
    assert table["rows"][1]["outbound_count"] == "4"
    assert table["rows"][2]["moving_average"] == "371.25"
    assert table["rows"][-1]["cumulative_quantity"] == "1113.75"
    assert len(queries) == 1 and "allocate_qty" not in queries[0]


def test_handler_does_not_fill_after_query_failure():
    request = RequestPlanner().classify(question(START, END, 3))

    class Broken:
        legacy_owner = "HJXH"
        def query(self, *args, **kwargs):
            raise SemanticRuntimeError("synthetic", reason="query_timeout")

    with pytest.raises(SemanticRuntimeError, match="synthetic"):
        SalesTimeSeriesHandler(Broken(), timezone_name="Asia/Shanghai").run(request)
