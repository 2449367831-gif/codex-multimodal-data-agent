from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent_core.planning import RequestPlanner
from app.agent_core.planner import intent_from_request
from app.agent_core.order_return_planning import return_clarification, return_block_reason
from app.codex_agent import reviewed_clarification_for_question


NOW = datetime(2026, 9, 11, 12, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


@pytest.fixture(autouse=True)
def approved(monkeypatch):
    monkeypatch.setattr('app.agent_core.order_return_planning.return_quantity_contract_active', lambda _: True)


def planner():
    return RequestPlanner(clock=lambda: NOW)


def selections(**values):
    return [dict(question_id=key, option_id=value) for key, value in values.items()]


def test_identifiers_are_exact_and_masked_before_dates():
    request = planner().classify('查询货主 B 销售订单号 `HJXH-2026-08-03-SO07` 的退货数量，SKU `001-AbC`')
    assert request.route == 'sales_order_net_quantity'
    assert request.order_return_plan.order_codes == ('HJXH-2026-08-03-SO07',)
    assert request.order_return_plan.sku_codes == ('001-AbC',)
    assert request.scope.owner == 'B' and request.scope.unit == 'source_quantity'
    assert request.start is None and request.end is None
    intent = intent_from_request(request)
    assert intent.grain == 'owner+order+sku'
    assert intent.scope['order_codes'] == ['HJXH-2026-08-03-SO07']
    from app.agent_core.capsule import ScopeCapsule
    assert ScopeCapsule.from_intent(intent).as_dict()['order_codes'] == ['HJXH-2026-08-03-SO07']


def test_quoted_identifiers_are_not_unicode_normalized():
    request = planner().classify('查询销售订单 `Ａ-01` 的退货数量，SKU `Ｂ-02`')
    assert request.order_return_plan.order_codes == ('Ａ-01',)
    assert request.order_return_plan.sku_codes == ('Ｂ-02',)


def test_month_first_day_does_not_offer_empty_midnight_window(monkeypatch):
    from app.agent_core.order_return_planning import _parse, _questions
    parsed = _parse('查询本月销售订单退货数量')
    card = _questions(parsed, NOW.replace(day=1))
    assert 'return_month_midnight' not in {o['id'] for o in card[0]['options']}


def test_month_start_does_not_offer_empty_question_time_window():
    from app.agent_core.order_return_planning import _parse, _questions
    card = _questions(_parse('查询本月销售订单退货数量'), NOW.replace(day=1, hour=0, minute=0))
    assert [o['id'] for o in card[0]['options']] == ['return_month_full']


@pytest.mark.parametrize('selected', [[], None, [{}], 'invalid',
    selections(return_measure='return_quantity_only') * 2])
def test_invalid_selection_reason_is_stable(selected):
    reason = return_block_reason('查询销售订单退货金额', contract_root=None, selected=selected)
    assert reason is None if selected is None else isinstance(reason, str)


def test_unspecified_scope_is_all_not_inherited():
    p = planner()
    assert p.classify('查询昨天货主 A 销售订单 S1 的退货数量，SKU X').scope.owner == 'A'
    request = p.classify('查询销售订单退货数量')
    assert request.scope.owner == 'ALL'
    assert request.order_return_plan.order_codes == request.order_return_plan.sku_codes == ()
    assert request.start is None


def test_yesterday_is_order_creation_natural_day():
    request = planner().classify('查询昨天销售订单的实发数量、退货数量和净销售数量')
    assert request.start == NOW.replace(day=10, hour=0, minute=0)
    assert request.end == NOW.replace(hour=0, minute=0)


def test_explicit_range_excludes_end_midnight():
    request = planner().classify('查询2026-09-01至2026-09-10 0点前销售订单的退货数量')
    assert request.start == NOW.replace(day=1, hour=0, minute=0)
    assert request.end == NOW.replace(day=10, hour=0, minute=0)


def test_month_and_amount_are_asked_together_and_bound_once():
    question = '查询本月销售订单的退货数量和退货金额'
    assert planner().classify(question) is None
    card = reviewed_clarification_for_question(question)
    assert [q['id'] for q in card['questions']] == ['return_measure', 'return_month_window']
    assert all(len({o['label'] for o in q['options']}) == len(q['options']) for q in card['questions'])
    request = planner().plan_selected(question, selections(return_measure='return_quantity_only',
        return_month_window='return_month_now'), reference_time=NOW)
    assert request.start == NOW.replace(day=1, hour=0, minute=0)
    assert request.end == NOW
    assert request.order_return_plan.start == request.start


def test_selection_uses_original_time_after_midnight():
    original = NOW.replace(day=30, hour=23, minute=59)
    request = planner().plan_selected('查询本月销售订单退货数量',
        selections(return_month_window='return_month_midnight'), reference_time=original)
    assert request.start == original.replace(day=1, hour=0, minute=0)
    assert request.end == original.replace(hour=0, minute=0)


@pytest.mark.parametrize('extra', ['仅限仓库 W1', '仅限客户 C1', '只要成品', '退货时间昨天',
    '数量大于10', '按日期汇总', '换算箱数', '本月上月', '2026-13-01', '最近24小时',
    '排除SKU X', '只查一个订单', '只查最新订单', '2026-09-10至2026-09-01',
    '2026-09-01至2026-09-01 0点'])
def test_unsupported_conditions_are_not_erased(extra):
    question = '查询销售订单退货数量，' + extra
    assert planner().classify(question) is None
    assert return_clarification(question, contract_root=None) is None
    assert return_block_reason(question, contract_root=None)


def test_unpublished_never_offers_executable_choices(monkeypatch):
    monkeypatch.setattr('app.agent_core.order_return_planning.return_quantity_contract_active', lambda _: False)
    question = '查询本月销售订单退货数量'
    assert planner().classify(question) is None
    assert return_clarification(question, contract_root=None) is None
    assert '尚未审批发布' in return_block_reason(question, contract_root=None)


def test_money_selection_is_not_silently_reduced_to_quantity():
    question = '查询销售订单 S1 的退货金额'
    selected = selections(return_measure='return_actual_refund')
    assert planner().plan_selected(question, selected) is None
    assert '金额' in return_block_reason(question, contract_root=None, selected=selected)


@pytest.mark.parametrize('selected', [
    selections(return_month_window='return_month_now'),
    selections(return_measure='return_quantity_only'),
    selections(return_measure='return_quantity_only', return_month_window='return_month_now', output_grain='sku'),
    selections(return_measure='bad', return_month_window='return_month_now'),
])
def test_missing_unknown_or_extra_decisions_never_execute(selected):
    assert planner().plan_selected('查询本月销售订单退货金额', selected) is None


def test_explanations_do_not_become_data_queries():
    question = '请解释销售订单退货金额的计算规则'
    assert planner().classify(question) is None
    assert return_block_reason(question, contract_root=None) is None
