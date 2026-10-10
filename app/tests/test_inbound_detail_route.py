"""Receipt routing requirements fixed before live result verification."""

from datetime import date, datetime
from pathlib import Path

import pytest

from app.agent_core.planning import RequestPlanner
from app.agent_core.receipt_planning import inbound_detail_request
from app.query_scope import ScopeResolver


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 10, 14, 20)
QUESTION = "查询最近7个完整自然日生产日期的采购入库明细，但不包含今天完成入库的数据。返回仓库、入库单、SKU、生产日期、实收入库数量和实际入库时间。"


def plan(question):
    return inbound_detail_request(question, today=NOW.date(), resolver=ScopeResolver(), contract_root=ROOT,
                                  interval_parser=RequestPlanner(clock=lambda: NOW)._source_interval)


def test_purchase_type_is_default_without_a_redundant_question():
    request = plan(QUESTION)
    assert request.route == "purchase_received_detail"
    assert (request.start, request.end, request.receipt_plan.arrival_before) == (date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 10))
    assert request.receipt_plan.purchase_basis == "purchase_type"
    assert request.scope.owner == "ALL"
    assert not request.scope.finished_goods and not request.scope.warehouse_codes


@pytest.mark.parametrize("suffix,basis", [
    ("按采购单号非空识别", "purchase_order_reference"),
    ("按入库类型=10识别", "purchase_type"),
    ("按采购单号非空且入库类型=10识别", "both"),
])
def test_explicit_variant_changes_only_procurement_predicate(suffix, basis):
    request = plan(QUESTION + suffix)
    assert request.receipt_plan.purchase_basis == basis
    assert request.receipt_plan.arrival_before == date(2026, 9, 10)


@pytest.mark.parametrize("suffix", [
    "只查询供应商甲", "仅限云仓", "只看数量大于100的", "入库类型=11",
    "只查客户A", "按吨返回", "以采购单价算金额", "今天入库的数据也要包含",
    "只查库存不足的SKU", "排除HJXH", "同时统计销售出库量",
])
def test_unimplemented_predicate_cannot_be_dropped(suffix):
    assert plan(QUESTION + suffix) is None


def test_missing_second_time_axis_is_not_guessed():
    assert plan("查询最近7个完整自然日生产日期的采购入库明细") is None


def test_owner_does_not_carry_into_next_question():
    assert plan("HJXH货主" + QUESTION).scope.owner == "HJXH"
    assert plan("HJXH货主" + QUESTION).receipt_plan.owner == "HJXH"
    assert plan(QUESTION).scope.owner == "ALL"
    assert plan(QUESTION).receipt_plan.owner == "ALL"


def test_published_approval_required(tmp_path):
    assert inbound_detail_request(QUESTION, today=NOW.date(), resolver=ScopeResolver(), contract_root=tmp_path,
                                  interval_parser=RequestPlanner(clock=lambda: NOW)._source_interval) is None


def test_unsupported_receipt_detail_never_falls_into_arrival_summary():
    planner = RequestPlanner(clock=lambda: NOW)
    assert planner.classify("查询最近7个完整自然日生产日期的采购入库明细，按供应商汇总数量") is None
