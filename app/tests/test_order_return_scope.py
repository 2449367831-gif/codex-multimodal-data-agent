from dataclasses import replace

import pytest

from app.agent_core.capsule import ScopeCapsule
from app.agent_core.order_components import OrderComponentPlan
from app.agent_core.planner import intent_from_request
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.codex_agent import reviewed_clarification_for_question
from app.query_scope import QueryScope
from app.request_context import RequestContextStore
from app.tests.test_order_return_planning import planner, selections, approved
from app.tests.test_order_return_reporting import Query, published
from app.tests.test_order_components import facts as order_facts, header, detail
from app.tests.test_return_components import facts as return_facts, header as rh, detail as rd


def test_multiple_explicit_lists_reach_typed_scope():
    request = planner().classify('查询货主 A、B 销售订单 S1、S2 的退货数量，SKU X、Y')
    assert request is not None
    assert request.order_return_plan.owner_codes == ('A', 'B')
    assert request.order_return_plan.order_codes == ('S1', 'S2')
    assert request.order_return_plan.sku_codes == ('X', 'Y')
    intent = intent_from_request(request)
    assert intent.scope['owner'] == 'SELECTED'
    assert intent.scope['owner_codes'] == ['A', 'B']
    assert ScopeCapsule.from_intent(intent).as_dict()['owner_codes'] == ['A', 'B']
    assert planner().classify('查询销售订单退货数量').scope.owner == 'ALL'


def test_identifier_values_are_opaque_to_other_field_parsers():
    request = planner().classify('查询货主 `A SKU X` 销售订单 `S1 货主 B` 的退货数量，SKU `X, Y`')
    assert request is not None
    assert request.order_return_plan.owner_codes == ('A SKU X',)
    assert request.order_return_plan.order_codes == ('S1 货主 B',)
    assert request.order_return_plan.sku_codes == ('X, Y',)


def test_all_conflicts_and_amount_choices_are_returned_together():
    question = '查询所有货主、货主 A、B，全部销售订单、销售订单 S1，全部SKU、SKU X，全部时间、本月的退货金额'
    assert planner().classify(question) is None
    card = reviewed_clarification_for_question(question)
    assert card is not None
    assert {q['id'] for q in card['questions']} == {
        'return_measure', 'return_owner_scope', 'return_order_scope', 'return_sku_scope', 'return_date_scope'}
    request = planner().plan_selected(question, selections(return_measure='return_quantity_only',
        return_owner_scope='return_owner_list', return_order_scope='return_order_list',
        return_sku_scope='return_sku_list', return_date_scope='return_month_full'))
    assert request.order_return_plan.owner_codes == ('A', 'B')
    assert request.order_return_plan.order_codes == ('S1',)
    assert request.order_return_plan.sku_codes == ('X',)
    assert request.start.month == 9 and request.end.month == 10
    request = planner().plan_selected(question, selections(return_measure='return_quantity_only',
        return_owner_scope='return_owner_all', return_order_scope='return_order_all',
        return_sku_scope='return_sku_all', return_date_scope='return_date_all'))
    assert request.order_return_plan == OrderComponentPlan()


@pytest.mark.parametrize('question', [
    '查询货主 A 销售订单 S1、货主 B 销售订单 S2 的退货数量',
    '查询货主 A SKU X、货主 B SKU Y 销售订单的退货数量',
    '查询销售订单 S1 SKU X、销售订单 S2 SKU Y 的退货数量',
])
def test_associated_pairs_are_not_flattened_into_cartesian_scope(question):
    assert planner().classify(question) is None


def test_code_list_stops_before_next_field_label():
    request = planner().classify('查询货主 A、B，SKU X、Y，销售订单 S1 的退货数量')
    assert request.order_return_plan.owner_codes == ('A', 'B')
    assert request.order_return_plan.sku_codes == ('X', 'Y')
    assert request.order_return_plan.order_codes == ('S1',)


def test_multiple_owners_keep_same_order_and_sku_separate(order_facts, return_facts):
    for owner, shipped, returned in [('A', 10, 3), ('B', 20, 4), ('C', 40, 5)]:
        header(order_facts, owner=owner)
        detail(order_facts, owner=owner, quantity=shipped)
        rh(return_facts, owner=owner)
        rd(return_facts, owner=owner, quantity=returned)
    query = Query(order_facts, return_facts)
    runtime = SemanticQueryRuntime(query_client=query)
    result = runtime.run_if_supported('查询货主 A、B 销售订单 S1 的退货数量')
    assert result['query_plan']['scope']['owner_codes'] == ['A', 'B']
    tables = result['business_tables']
    assert len(tables[0]['rows']) == 2
    assert {r['cargo_owner']: r['net_sales_quantity'] for r in tables[1]['rows']} == {'A': '7', 'B': '16'}
    assert all('全部货主' not in table['caption'] for table in tables)
    assert all('A' in table['caption'] and 'B' in table['caption'] for table in tables)


def test_mismatched_multiowner_scope_stops_before_any_query(order_facts, return_facts):
    request = planner().classify('查询货主 A、B 销售订单 S1 的退货数量')
    assert request is not None
    query = Query(order_facts, return_facts)
    with pytest.raises(SemanticRuntimeError):
        SemanticQueryRuntime(query_client=query).execute_plan(replace(request, scope=QueryScope(owner='A')),
                                                              question='query')
    assert not query.calls


def test_typed_return_scope_cannot_execute_another_route(order_facts, return_facts):
    request = planner().classify('查询货主 A、B 销售订单 S1 的退货数量')
    query = Query(order_facts, return_facts)
    with pytest.raises(SemanticRuntimeError) as failure:
        SemanticQueryRuntime(query_client=query).execute_plan(
            replace(request, route='current_inventory'), question='query')
    assert failure.value.reason == 'order_return_scope_invalid'
    assert not query.calls


def test_literal_all_owner_is_not_unrestricted(order_facts, return_facts):
    for owner in ('ALL', 'A'):
        header(order_facts, owner=owner)
        detail(order_facts, owner=owner)
        rh(return_facts, owner=owner)
        rd(return_facts, owner=owner)
    runtime = SemanticQueryRuntime(query_client=Query(order_facts, return_facts))
    selected = runtime.run_if_supported('查询货主 ALL 销售订单 S1 的退货数量')
    assert selected['query_plan']['scope']['owner'] == 'SELECTED'
    assert selected['query_plan']['scope']['owner_codes'] == ['ALL']
    assert [row['cargo_owner'] for row in selected['business_tables'][1]['rows']] == ['ALL']
    assert '全部货主' not in selected['business_tables'][1]['caption']
    unrestricted = runtime.run_if_supported('查询销售订单 S1 的退货数量')
    assert unrestricted['query_plan']['scope']['owner'] == 'ALL'
    assert 'owner_codes' not in unrestricted['query_plan']['scope']
    assert {row['cargo_owner'] for row in unrestricted['business_tables'][1]['rows']} == {'ALL', 'A'}


def test_one_selected_owner_reference_conflict_stops_whole_result(order_facts, return_facts):
    for owner in ('A', 'B'):
        header(order_facts, owner=owner)
        detail(order_facts, owner=owner)
        rh(return_facts, owner=owner)
        rd(return_facts, owner=owner, order='OTHER' if owner == 'B' else 'S1')
    runtime = SemanticQueryRuntime(query_client=Query(order_facts, return_facts))
    with pytest.raises(SemanticRuntimeError) as failure:
        runtime.run_if_supported('查询货主 A、B 销售订单 S1 的退货数量')
    assert failure.value.reason == 'return_component_quality_failed'


def test_history_retains_exact_scope_without_rows_or_cross_owner_access(tmp_path):
    request = planner().classify('查询货主 A、B 销售订单 S1、S2 的退货数量，SKU X、Y')
    plan = {'route': request.route, 'scope': request.order_return_plan.scope_metadata(),
            'rows': [{'not_memory': 'synthetic'}], 'sql': 'SELECT 1'}
    store = RequestContextStore(tmp_path / 'requests.sqlite3')
    store.record('installation-a', 'request-a', 'synthetic question', plan, 'completed')
    saved = store.recent('installation-a')[0]['plan']
    assert saved['scope']['owner_codes'] == ['A', 'B']
    assert saved['scope']['order_codes'] == ['S1', 'S2']
    assert saved['scope']['sku_codes'] == ['X', 'Y']
    assert 'rows' not in saved and 'sql' not in saved
    assert store.recent('installation-b') == []
