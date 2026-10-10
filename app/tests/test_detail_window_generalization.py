"""Equivalent calendar requests must reach the same governed detail readers."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.agent_core.planning import RequestPlanner
from app.agent_core.sales_detail_partitions import SalesDetailPartitionPlan, retrieve_partitioned_sales_details
from app.tests.test_sales_detail_partitions import query_fixture
from app.tests.test_sales_aggregation import facts
from app.tests.test_inbound_details import receipts, execute
from app.agent_core.inbound_details import reconcile_inbound_details


TODAY = date(2026, 9, 10)


def planner():
    return RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))


def receipt_question(window):
    return f"查询{window}生产日期的采购入库明细，但不包含今天完成入库的数据。"


@pytest.mark.parametrize("prefix", ["近", "最近", "过去", "前"])
@pytest.mark.parametrize("days", [1, 7, 14, 30, 90, 366, 3650])
@pytest.mark.parametrize("route", ["sales_outbound_details", "purchase_received_detail"])
def test_day_count_is_a_parameter_not_a_route_name(prefix, days, route):
    window = f"{prefix}{days}个完整自然日"
    question = receipt_question(window) if route == "purchase_received_detail" else f"查询{window}实际销售出库明细"
    request = planner().classify(question)
    assert request is not None and request.route == route
    assert (request.start, request.end) == (TODAY - timedelta(days=days), TODAY)
    assert request.scope.owner == "ALL" and not request.scope.sku_codes and not request.scope.warehouse_codes
    if request.receipt_plan:
        assert request.receipt_plan.arrival_before == TODAY
        assert request.receipt_plan.purchase_basis == "purchase_type"


@pytest.mark.parametrize("window,start,end", [
    ("过去两天", date(2026, 9, 8), TODAY),
    ("最近十四天", date(2026, 8, 27), TODAY),
    ("2026年8月1日至9月1日", date(2026, 8, 1), date(2026, 9, 2)),
    ("2026/08/01至2026/09/01 0 点前", date(2026, 8, 1), date(2026, 9, 1)),
    ("2024年2月28日至2月29日", date(2024, 2, 28), date(2024, 3, 1)),
    ("2025年12月31日至2026年1月2日", date(2025, 12, 31), date(2026, 1, 3)),
    ("上上个月", date(2026, 7, 1), date(2026, 8, 1)),
    ("2026年8月", date(2026, 8, 1), date(2026, 9, 1)),
    ("上个季度", date(2026, 4, 1), date(2026, 7, 1)),
    ("今年", date(2026, 1, 1), TODAY),
])
@pytest.mark.parametrize("route", ["sales_outbound_details", "purchase_received_detail"])
def test_calendar_notation_preserves_complete_interval(window, start, end, route):
    question = receipt_question(window) if route == "purchase_received_detail" else f"查询{window}实际销售出库明细"
    request = planner().classify(question)
    assert request is not None and request.route == route
    assert (request.start, request.end) == (start, end)
    if request.receipt_plan:
        assert request.receipt_plan.arrival_before == TODAY


@pytest.mark.parametrize("question", [
    "昨天实际销售出库明细，用表格展示",
    "用中文表格展示昨天实际销售出库明细",
    "实际销售出库明细，昨天，显示表格",
    "查询昨天实际销售出库明细，用表格的形式展示。",
])
def test_presentation_and_word_order_do_not_change_the_business_query(question):
    request = planner().classify(question)
    assert request is not None and request.route == "sales_outbound_details"
    assert (request.start, request.end) == (date(2026, 9, 9), TODAY)


@pytest.mark.parametrize("window", ["近0天", "近3651天", "近10000天", "近7天和近14天", "昨天和上月",
                                          "2026-09-01 12:00至2026-09-10 00:00"])
def test_invalid_conflicting_and_intraday_windows_are_not_reduced_to_one_day(window):
    from app.agent_core.sales_detail_planning import sales_detail_request
    assert sales_detail_request(f"查询{window}实际销售出库明细", planner=planner()) is None
    assert planner().classify(receipt_question(window)) is None


@pytest.mark.parametrize("suffix", ["排除客户甲", "数量大于5", "按订单创建时间", "换算成箱", "按供应商汇总"])
def test_window_generalization_does_not_consume_unimplemented_predicates(suffix):
    from app.agent_core.sales_detail_planning import sales_detail_request
    assert sales_detail_request(f"近14天实际销售出库明细，{suffix}", planner=planner()) is None
    assert planner().classify(receipt_question("近14天") + suffix) is None


def test_sku_codes_are_opaque_to_calendar_extraction_and_scope_does_not_carry():
    service = planner()
    request = service.classify("近14天HJXH长沙黄兴SKU SKU-2026-01-02实际销售出库明细")
    assert request is not None
    assert request.scope.sku_codes == ("SKU-2026-01-02",)
    assert request.scope.owner == "HJXH" and request.scope.warehouse_codes == ("10114790",)
    assert (request.start, request.end) == (date(2026, 8, 27), TODAY)
    following = service.classify("近14天实际销售出库明细")
    assert following.scope.owner == "ALL" and not following.scope.sku_codes and not following.scope.warehouse_codes


@pytest.mark.parametrize("days,expected", [(1, "18"), (7, "1018"), (14, "1068")])
def test_sales_window_reaches_every_month_and_control_without_changing_grain(facts, days, expected):
    facts.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES ('HJXH','AUG','W1','2026-08-31 23:59:59',700,'JYCK','S1','20',0,'false')")
    facts.execute("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES ('HJXH','AUG','A',50,0,'false')")
    request = planner().classify(f"近{days}天实际销售出库明细")
    assert request is not None
    query, _ = query_fixture(facts)
    result = retrieve_partitioned_sales_details(SalesDetailPartitionPlan(request.start, request.end, request.scope), query)
    assert result.complete and result.summary.quantity == Decimal(expected)
    assert sum(Decimal(str(row["quantity"])) for row in result.rows) == Decimal(expected)
    assert all(request.start <= datetime.fromisoformat(row["ship_at"]).date() < request.end for row in result.rows)


@pytest.mark.parametrize("days,expected", [(7, "16.5"), (14, "116.5")])
def test_production_window_changes_without_including_today_arrivals(receipts, days, expected):
    request = planner().classify(receipt_question(f"近{days}天"))
    assert request is not None
    result = reconcile_inbound_details(execute(receipts, request.receipt_plan), request.receipt_plan)
    assert result.totals_by_unit == {"10": Decimal(expected)}
    assert not {"TODAY", "FUTURE", "REF_ONLY"}.intersection(row["inbound_code"] for row in result.rows)
