from datetime import datetime, date
from itertools import product

import pytest

from app.agent_core.planning import RequestPlanner
from app.codex_agent import reviewed_clarification_for_question


QUESTION = '业务只说“昨天出库量”，但未说明是销售出库、所有出库、实发数量、分配数量还是订单量。请列出需要确认的口径，并说明默认不能猜哪个字段。'
OPTIONS = {
    'outbound_business': ('outbound_sales', 'outbound_transfer', 'outbound_adjustment', 'outbound_other', 'outbound_all'),
    'outbound_measure': ('outbound_actual_qty', 'outbound_allocated_qty', 'outbound_requested_qty', 'outbound_document_count', 'outbound_order_count'),
    'outbound_time_axis': ('outbound_shipped_at', 'outbound_order_created_at', 'outbound_reviewed_at'),
}


def answers(values):
    return [{'question_id': key, 'option_ids': [value]} for key, value in zip(OPTIONS, values)]


@pytest.mark.parametrize('question', [QUESTION, '昨天出库量', '查询昨日出库量。', '请统计昨天的出库数量'])
def test_all_decisions_once_without_guessing(question):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    assert planner.classify(question) is None
    card = reviewed_clarification_for_question(question)
    assert card['outcome'] == 'clarification_required'
    assert {q['id']: tuple(o['id'] for o in q['options']) for q in card['questions']} == OPTIONS
    assert all(q['selection'] == 'single' for q in card['questions'])
    assert all('该选项' != o['label'] for q in card['questions'] for o in q['options'])
    unavailable = {o['id'] for q in card['questions'] for o in q['options'] if o.get('available') is False}
    assert unavailable == {
        'outbound_adjustment', 'outbound_other',
        'outbound_allocated_qty', 'outbound_requested_qty',
        'outbound_order_count', 'outbound_order_created_at', 'outbound_reviewed_at',
    }


@pytest.mark.parametrize('values', list(product(*OPTIONS.values())))
def test_only_reviewed_combination_executes(values):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    request = planner.plan_selected(QUESTION, answers(values), context_reused=True)
    if values in {
        ('outbound_sales', 'outbound_actual_qty', 'outbound_shipped_at'),
        ('outbound_sales', 'outbound_document_count', 'outbound_shipped_at'),
        ('outbound_all', 'outbound_actual_qty', 'outbound_shipped_at'),
        ('outbound_all', 'outbound_document_count', 'outbound_shipped_at'),
    }:
        assert request.route == 'sales_aggregate'
        assert (request.start, request.end) == (date(2026, 9, 9), date(2026, 9, 10))
        assert request.sales_aggregation.dimensions == ()
        assert request.scope.owner == 'ALL' and not request.scope.warehouse_codes
        assert not request.scope.sku_codes and request.scope.channel == 'ALL'
        assert request.all_outbound_types is (values[0] == 'outbound_all')
    elif values == ('outbound_transfer', 'outbound_actual_qty', 'outbound_shipped_at'):
        assert request.route == 'transfer_outbound'
    else:
        assert request is None


@pytest.mark.parametrize('question', ['昨天出库量，只算沈阳仓', '昨天出库量按SKU排名', '昨天出库量不含退货', '今天出库量', '昨天订单量', QUESTION + ' 仅限HJXH'])
def test_selection_never_erases_unrecognized_scope(question):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    assert planner.plan_selected(question, answers(('outbound_sales', 'outbound_actual_qty', 'outbound_shipped_at'))) is None


@pytest.mark.parametrize('selection', [[], answers(('outbound_sales',)), answers(('outbound_sales', 'outbound_actual_qty')), answers(('outbound_sales', 'outbound_actual_qty', 'outbound_shipped_at')) + [{'question_id': 'scope', 'option_ids': ['all_owner']} ]])
def test_incomplete_or_extra_selection_not_executed(selection):
    assert RequestPlanner().plan_selected(QUESTION, selection) is None


def test_every_advertised_available_option_has_a_reviewed_execution_path():
    card = reviewed_clarification_for_question(QUESTION)
    available = {
        option['id']
        for question in card['questions']
        for option in question['options']
        if option.get('available', True)
    }
    assert available == {
        'outbound_sales', 'outbound_transfer', 'outbound_all',
        'outbound_actual_qty', 'outbound_document_count', 'outbound_shipped_at',
    }
    for measure in ('outbound_actual_qty', 'outbound_document_count'):
        selection = answers(('outbound_sales', measure, 'outbound_shipped_at'))
        assert RequestPlanner().plan_selected(QUESTION, selection).route == 'sales_aggregate'


@pytest.mark.parametrize('measure', ['outbound_actual_qty', 'outbound_document_count'])
@pytest.mark.parametrize('question,owner,warehouses,skus,finished,dimensions', [
    ('昨天HJXH出库量', 'HJXH', (), (), False, ()),
    ('昨天长沙黄兴的成品出库量', 'ALL', ('10114790',), (), True, ()),
    ('昨天SKU QY-YwKC500G出库量', 'ALL', (), ('QY-YwKC500G',), False, ()),
    ('昨天HJXH各仓库出库数量', 'HJXH', (), (), False, ('warehouse',)),
    ('昨天出库量，按SKU汇总', 'ALL', (), (), False, ('sku',)),
    ('昨天出库量，按货主、仓库、SKU汇总', 'ALL', (), (), False, ('cargo_owner', 'warehouse', 'sku')),
    ('查询昨天所有货主所有仓库出库量', 'ALL', (), (), False, ()),
])
def test_scoped_question_keeps_its_scope_and_grain_after_one_selection(
    question, owner, warehouses, skus, finished, dimensions, measure,
):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    assert planner.classify(question) is None
    card = reviewed_clarification_for_question(question)
    assert {q['id'] for q in card['questions']} == set(OPTIONS)
    request = planner.plan_selected(question, answers(('outbound_sales', measure, 'outbound_shipped_at')))
    assert request is not None
    assert request.scope.owner == owner
    assert request.scope.warehouse_codes == warehouses
    assert request.scope.sku_codes == skus
    assert request.scope.finished_goods == finished
    assert request.scope.channel == 'ALL'
    assert request.sales_aggregation.dimensions == dimensions
    assert (request.start, request.end) == (date(2026, 9, 9), date(2026, 9, 10))
    following = planner.plan_selected('昨天出库量', answers(('outbound_sales', measure, 'outbound_shipped_at')), context_reused=True)
    assert following.scope.owner == 'ALL' and not following.scope.warehouse_codes
    assert not following.scope.sku_codes and not following.scope.finished_goods
    assert following.sales_aggregation.dimensions == ()


@pytest.mark.parametrize('question', [
    '昨天HJXH所有货主出库量', '昨天长沙黄兴所有仓库出库量',
    '昨天SKU QY-YwKC500G所有SKU出库量', '昨天HJXH出库量，单位为吨',
    '昨天HJXH出库量排除客户甲', '昨天长沙黄兴出库量不含退货',
    '昨天出库量，按SKU排名', '昨天出库量，数量大于10',
])
def test_scope_extension_does_not_guess_conflicts_or_erase_unsupported_predicates(question):
    from app.agent_core.outbound_clarification import outbound_clarification
    assert outbound_clarification(question) is None
    assert RequestPlanner().plan_selected(question, answers(('outbound_sales', 'outbound_actual_qty', 'outbound_shipped_at'))) is None


def test_available_document_count_has_no_unavailable_label():
    card = reviewed_clarification_for_question('昨天出库量')
    option = next(o for q in card['questions'] for o in q['options'] if o['id'] == 'outbound_document_count')
    assert option['available'] is True and option['label'] == '出库单数'
