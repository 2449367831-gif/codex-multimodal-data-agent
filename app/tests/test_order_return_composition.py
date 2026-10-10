from copy import deepcopy
from fractions import Fraction

import pytest

from app.agent_core.order_return_composition import compose_order_returns, retrieve_order_returns
from app.agent_core.order_components import OrderComponentPlan
from app.tests.test_order_components import read
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_order_components import facts as order_facts, header as sale_header, detail as sale_detail, result as sales
from app.tests.test_return_components import facts as return_facts, header as return_header, detail as return_detail, result as returns


def test_multiple_sale_lines_and_multiple_returns_do_not_repeat_deduction(order_facts, return_facts):
    sale_header(order_facts)
    sale_detail(order_facts, quantity=6, payment=36)
    sale_detail(order_facts, identity='D2', quantity=4, payment=24)
    return_header(return_facts)
    return_header(return_facts, code='R2')
    return_detail(return_facts, quantity=2)
    return_detail(return_facts, identity='R2D', code='R2', quantity=1)
    checked = compose_order_returns(sales(order_facts), returns(return_facts), amount_mode='proportional')
    row = checked.groups[0]
    assert row['order_ship_qty'] == 10 and row['returned_qty'] == 3 and row['net_qty'] == 7
    assert row['return_document_count'] == 2
    assert row['proportional_payment'] == 42 and row['proportional_deduction'] == 18
    assert row['original_detail_payment'] == 60
    assert checked.orders[0]['header_payment'] == 100


def test_pending_amount_choice_does_not_guess_refund(order_facts, return_facts):
    sale_header(order_facts)
    sale_detail(order_facts)
    row = compose_order_returns(sales(order_facts), returns(return_facts)).groups[0]
    assert row['net_qty'] == 10 and row['returned_qty'] == 0
    assert row['proportional_payment'] is None
    assert 'amount_choice_pending' in row['issues']
    assert 'actual_refund' not in row


def test_zero_denominator_keeps_historical_payment_and_warns(order_facts, return_facts):
    sale_header(order_facts)
    sale_detail(order_facts, quantity=0, payment=20)
    return_header(return_facts)
    return_detail(return_facts, quantity=2)
    row = compose_order_returns(sales(order_facts), returns(return_facts), amount_mode='proportional').groups[0]
    assert row['net_qty'] == -2
    assert row['proportional_payment'] == 20
    assert {'zero_shipment_kept_payment', 'returns_exceed_shipment'} <= set(row['issues'])


def test_excess_return_is_preserved_and_flagged(order_facts, return_facts):
    sale_header(order_facts)
    sale_detail(order_facts, quantity=2, payment=10)
    return_header(return_facts)
    return_detail(return_facts, quantity=3)
    row = compose_order_returns(sales(order_facts), returns(return_facts), amount_mode='proportional').groups[0]
    assert row['net_qty'] == -1 and row['proportional_payment'] == -5
    assert 'returns_exceed_shipment' in row['issues']


def test_partial_return_proration_preserves_exact_fraction(order_facts, return_facts):
    sale_header(order_facts)
    sale_detail(order_facts, quantity=3, payment=1)
    return_header(return_facts)
    return_detail(return_facts, quantity=1)
    row = compose_order_returns(sales(order_facts), returns(return_facts), amount_mode='proportional').groups[0]
    assert row['proportional_payment'] == Fraction(2, 3)
    assert row['proportional_deduction'] == Fraction(1, 3)


def test_order_without_sale_sku_preserves_unmatched_return(order_facts, return_facts):
    sale_header(order_facts)
    return_header(return_facts)
    return_detail(return_facts)
    checked = compose_order_returns(sales(order_facts), returns(return_facts))
    assert len(checked.orders) == 1
    assert checked.groups[0]['returned_qty'] == 3
    assert checked.groups[0]['order_ship_qty'] is None and checked.groups[0]['net_qty'] is None
    assert 'missing_sales_sku' in checked.groups[0]['issues']


@pytest.mark.parametrize('defect', ['other_owner', 'other_order', 'duplicate_group'])
def test_return_scope_or_duplicates_cannot_silently_join(order_facts, return_facts, defect):
    sale_header(order_facts)
    sale_detail(order_facts)
    return_header(return_facts, owner='B' if defect == 'other_owner' else 'A', order='OTHER' if defect == 'other_order' else 'S1')
    return_detail(return_facts, owner='B' if defect == 'other_owner' else 'A', order='OTHER' if defect == 'other_order' else 'S1')
    checked = returns(return_facts)
    if defect == 'duplicate_group': checked.groups.append(deepcopy(checked.groups[0]))
    with pytest.raises(SemanticRuntimeError): compose_order_returns(sales(order_facts), checked)


def test_empty_is_not_an_all_scope_retry(order_facts, return_facts):
    checked = compose_order_returns(sales(order_facts), returns(return_facts))
    assert checked.groups == [] and checked.orders == []


@pytest.mark.parametrize('missing', ['payment', 'currency'])
def test_missing_monetary_evidence_cannot_become_zero(order_facts, return_facts, missing):
    sale_header(order_facts, currency=None if missing == 'currency' else 'CNY')
    sale_detail(order_facts, payment=None if missing == 'payment' else 60)
    row = compose_order_returns(sales(order_facts), returns(return_facts), amount_mode='proportional').groups[0]
    assert row['net_qty'] == 10 and row['proportional_payment'] is None


def test_actual_refund_mode_is_not_a_proportional_alias(order_facts, return_facts):
    with pytest.raises(ValueError): compose_order_returns(sales(order_facts), returns(return_facts), amount_mode='actual_refund')


def test_full_retrieval_pages_exact_cohort_and_checks_both_sources(order_facts, return_facts):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    for owner in ('A', 'B'):
        for order in ('S1', 'S2'):
            selected = (owner, order) in (('A', 'S1'), ('B', 'S2'))
            sale_header(order_facts, owner=owner, order=order,
                        created='2026-09-01 00:00:00' if selected else '2026-08-01 00:00:00')
            sale_detail(order_facts, owner=owner, order=order, identity=order)
            return_header(return_facts, owner=owner, order=order, code=order)
            return_detail(return_facts, owner=owner, order=order, code=order, identity=order)
    calls = []
    def query(sql, **kwargs):
        is_sales = 'scm_sales_order_header' in sql
        calls.append(is_sales)
        return read(order_facts if is_sales else return_facts, sql)
    zone = ZoneInfo('Asia/Shanghai')
    plan = OrderComponentPlan(start=datetime(2026, 9, 1, tzinfo=zone), end=datetime(2026, 10, 1, tzinfo=zone))
    checked = retrieve_order_returns(plan, query, page_size=2)
    assert {(r['cargo_owner'], r['sales_order_code']) for r in checked.groups} == {('A', 'S1'), ('B', 'S2')}
    assert all(r['net_qty'] == 7 for r in checked.groups)
    assert sum(calls) > 4 and len(calls) - sum(calls) > 4


def test_empty_retrieval_never_queries_all_returns(order_facts, return_facts):
    return_header(return_facts)
    return_detail(return_facts)
    def query(sql, **kwargs):
        assert 'scm_return_order' not in sql
        return read(order_facts, sql)
    assert retrieve_order_returns(OrderComponentPlan(), query).groups == []


@pytest.mark.parametrize('source', ['sales', 'returns'])
def test_source_change_between_component_reads_is_rejected(order_facts, return_facts, source):
    sale_header(order_facts)
    sale_detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts)
    last_source, sales_phases, changed = None, 0, False
    def query(sql, **kwargs):
        nonlocal last_source, sales_phases, changed
        current = 'sales' if 'scm_sales_order_header' in sql else 'returns'
        if current == 'sales' and last_source != current: sales_phases += 1
        if sales_phases == 2 and not changed:
            if source == 'sales': order_facts.execute('UPDATE ywzt_ods.scm_sales_order_detail SET ship_qty=11')
            else: return_facts.execute('UPDATE ywzt_ods.scm_return_order_detail SET fulfill_qty=4')
            changed = True
        last_source = current
        return read(order_facts if current == 'sales' else return_facts, sql)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_order_returns(OrderComponentPlan(), query)
    assert failure.value.reason == 'order_return_sources_changed'


def test_retrieval_keeps_row_cap_failure_distinct(order_facts, return_facts):
    sale_header(order_facts)
    sale_detail(order_facts)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_order_returns(OrderComponentPlan(), lambda sql, **kw: read(order_facts, sql), max_rows=2)
    assert failure.value.reason == 'result_truncated'


@pytest.mark.parametrize('header_order,detail_order', [('S1', 'CONFLICT'), ('OTHER', 'S1'), ('S1', None), (None, 'S1')])
def test_confirmed_reference_conflict_stops_entire_combined_query(order_facts, return_facts, header_order, detail_order):
    sale_header(order_facts)
    sale_detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts)
    return_header(return_facts, code='R2', order=header_order)
    return_detail(return_facts, identity='D2', code='R2', order=detail_order)
    def query(sql, **kwargs):
        return read(order_facts if 'scm_sales_order_header' in sql else return_facts, sql)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_order_returns(OrderComponentPlan(), query, amount_mode='proportional')
    assert failure.value.reason == 'return_component_quality_failed'
