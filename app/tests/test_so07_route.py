"""Requirements-first SO07 routing; no physical business source is queried."""

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.planning import RequestPlanner
from app.agent_core.planner import intent_from_request
from app.agent_core.sales_order_details import SalesOrderDetailPlan, retrieve_sales_order_details
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.tests.test_sales_order_details import orders, PLAN
from app.tests.test_sales_order_pages import Query


NOW = datetime(2026, 9, 11, 1, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
QUESTION = '查询今天的社团销售订单，订单号必须是 SO07，返回销售订单号、订单创建时间、客户、仓库、SKU、请求数量和订单状态。不要把普通 JYCK 订单混进来。'


@pytest.fixture
def planner(monkeypatch):
    monkeypatch.setattr('app.agent_core.so07_planning.so07_contract_active', lambda _: True)
    return RequestPlanner(clock=lambda: NOW)


def test_exact_acceptance_question_uses_order_type_and_created_natural_day(planner):
    request = planner.classify(QUESTION)
    assert request is not None and request.route == 'so07_order_details'
    assert (request.start, request.end) == (date(2026, 9, 11), date(2026, 9, 12))
    assert request.so07_plan.order_type == 'SO07' and request.ref_order_type is None
    assert request.scope.owner == 'ALL' and request.scope.unit == 'raw_source'
    assert intent_from_request(request).grain == 'owner+order+detail'


def test_scope_is_rebuilt_and_unmentioned_dates_are_all(planner):
    first = planner.classify('查询昨天货主 A 仓库编码 W1 SKU S1 的SO07订单请求数量')
    assert first and first.scope.owner == 'A'
    assert first.scope.warehouse_codes == ('W1',) and first.scope.sku_codes == ('S1',)
    next_request = planner.classify('查询SO07订单请求数量')
    assert next_request and next_request.start is None and next_request.end is None
    assert next_request.scope.owner == 'ALL' and not next_request.scope.sku_codes
    assert not next_request.scope.warehouse_codes and next_request.scope.channel == 'ALL'


def test_date_like_sku_cannot_become_date_filter(planner):
    result = planner.classify('查询SKU "2026-08-01" 的SO07订单请求数量')
    assert result and result.start is None and result.scope.sku_codes == ('2026-08-01',)


@pytest.mark.parametrize('period', ['2026-02-30至2026-09-10', '2026-09-11至2026-09-10',
                                   '2026-09-10至2026-09-10 0点', '2026-13-01'])
def test_invalid_dates_never_fall_back_to_one_day(planner, period):
    assert planner.classify(f'查询{period}的SO07订单请求数量') is None


def test_explicit_midnight_excludes_terminal_day(planner):
    request = planner.classify('查询2026-09-01至2026-09-10 0点前的SO07订单请求数量')
    assert request and request.start == date(2026,9,1) and request.end == date(2026,9,10)


@pytest.mark.parametrize('suffix', ['只要已付款', '只要成品', '客户 C1', '长沙黄兴', '按箱换算', '按出库时间', '退货金额', '订单状态 700', '最近24小时'])
def test_extra_conditions_are_not_discarded(planner, suffix):
    assert planner.classify('查询SO07订单请求数量，' + suffix) is None


@pytest.mark.parametrize('question', ['查询昨天实际销售出库', '查询DMS积分订单', '说明SO07和实际出库的区别'])
def test_unrelated_requests_not_owned_by_so07(question):
    from app.agent_core.so07_planning import is_so07_question
    assert not is_so07_question(question)


def test_all_conflicting_scopes_are_selected_together(planner):
    from app.agent_core.so07_planning import so07_clarification
    question = '查询本月全部时间全部货主货主 A 全部仓库仓库编码 W1 全部SKU SKU S1 的SO07订单请求数量'
    card = so07_clarification(question, contract_root=None, planner=planner)
    assert {q['id'] for q in card['questions']} == {'so07_date_scope', 'so07_owner_scope', 'so07_warehouse_scope', 'so07_sku_scope'}
    assert planner.classify(question) is None
    answers = [{'question_id': q['id'], 'option_ids': [q['options'][0]['id']]} for q in card['questions']]
    request = planner.plan_selected(question, answers, reference_time=NOW)
    assert request and request.scope.owner == 'A'
    assert request.scope.warehouse_codes == ('W1',) and request.scope.sku_codes == ('S1',)
    assert request.start == date(2026, 9, 1) and request.end == date(2026, 10, 1)


@pytest.mark.parametrize('day', [1, 11, 30])
def test_month_boundaries_offer_distinct_executable_windows(planner, day):
    from app.agent_core.so07_planning import so07_clarification
    first = NOW.replace(day=day)
    planner.clock = lambda: first
    question = '查询本月SO07订单请求数量'
    card = so07_clarification(question, contract_root=None, planner=planner)
    options = card['questions'][0]['options']
    assert len(options) >= 2
    intervals = set()
    for option in options:
        request = planner.plan_selected(question,[{'question_id':'so07_month_window','option_ids':[option['id']]}])
        assert request is not None
        intervals.add((request.start,request.end))
    assert len(intervals) == len(options)


def test_choice_uses_original_month_after_calendar_rollover(planner):
    planner.clock = lambda: NOW.replace(month=10, day=1)
    request = planner.plan_selected('查询本月SO07订单请求数量', [
        {'question_id':'so07_month_window','option_ids':['so07_month_including_today']}], reference_time=NOW)
    assert request and (request.start,request.end) == (date(2026,9,1), date(2026,9,12))


def test_full_time_reader_includes_prior_day_without_shipment_dependency(orders):
    plan = replace(PLAN, start=None, end=None)
    result = retrieve_sales_order_details(plan, Query(orders))
    assert result.order_count == 4
    assert {r['sales_order_code'] for r in result.rows} == {'SO1', 'SO2', 'SO3', 'OLD'}


@pytest.mark.parametrize('start,end', [(None, date(2026,9,11)), (date(2026,9,10), None)])
def test_half_bound_window_not_silently_widened(start, end):
    with pytest.raises(ValueError): SalesOrderDetailPlan(start, end)


def test_candidate_does_not_execute(planner, monkeypatch):
    from app.agent_core.so07_planning import so07_block_reason
    monkeypatch.setattr('app.agent_core.so07_planning.so07_contract_active', lambda _: False)
    assert planner.classify(QUESTION) is None
    assert '尚未审批发布' in so07_block_reason(QUESTION, contract_root=None)


def test_wrong_route_or_extra_fields_cannot_drop_typed_scope():
    from app.query_scope import QueryScope
    class NoQueries:
        def query(self, **kwargs): raise AssertionError('query must not run')
    runtime = SemanticQueryRuntime(query_client=NoQueries())
    request = SemanticRequest('so07_order_details', PLAN.start, PLAN.end,
                              scope=PLAN.execution_scope(), so07_plan=PLAN)
    for changed in (replace(request, route='sales_aggregate'), replace(request, finished_goods_only=True),
                    replace(request, scope=QueryScope()), replace(request, start=None)):
        with pytest.raises(SemanticRuntimeError): runtime.execute_plan(changed, question='')


def test_contract_gate_requires_matching_version_and_approval(tmp_path):
    import yaml
    from app.agent_core.handlers.so07 import so07_contract_active
    original = yaml.safe_load((Path(__file__).resolve().parents[2] / 'metrics/sales_order.so07_detail.yaml').read_text(encoding='utf8'))
    path = tmp_path / 'metrics/sales_order.so07_detail.yaml'
    path.parent.mkdir()
    for status, approval, version, expected in [('candidate','approved','1.0.0',False),
        ('active','pending_business_approval','1.0.0',False),('active','approved','2.0.0',False),
        ('active','approved','1.0.0',True)]:
        original.update(status=status, version=version)
        original['approval']['state'] = approval
        path.write_text(yaml.safe_dump(original),encoding='utf8')
        assert so07_contract_active(tmp_path) is expected
