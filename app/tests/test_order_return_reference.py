from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
import runpy
from zoneinfo import ZoneInfo

import pytest
import sqlglot
from sqlglot import exp

from app.agent_core.order_components import OrderComponentPlan
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_order_components import facts as order_facts, header, detail, read, result
from app.tests.test_return_components import facts as return_facts, header as return_header, detail as return_detail


@pytest.fixture(scope='module')
def reference():
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/acceptance/check_order_returns_live.py'))


def raw(reference, db, plan=None):
    plan = plan or OrderComponentPlan()
    return [read(db, reference['reference_sql'](plan, source)) for source in ('header', 'detail')]


def expected(reference, db, plan=None):
    plan = plan or OrderComponentPlan()
    return reference['expected_orders'](*raw(reference, db, plan), plan)


def query_both(orders, returns):
    return lambda sql, **kw: read(orders if 'scm_sales_order_' in sql else returns, sql)


def test_independent_order_reference_multiple_lines_currencies_and_missing(order_facts, reference):
    header(order_facts)
    header(order_facts, owner='B', currency='USD', payment=20)
    header(order_facts, order='NO_DETAIL', payment=None, currency=None)
    detail(order_facts, quantity=6, payment=36)
    detail(order_facts, identity='D2', quantity=4, payment=24)
    detail(order_facts, identity='D3', sku='002', payment=None, quantity=-1)
    detail(order_facts, owner='B', payment=20)
    order_facts.execute("UPDATE ywzt_ods.scm_sales_order_detail SET updated=NULL WHERE id='D2'")
    wanted = expected(reference, order_facts)
    assert wanted['groups'][0]['order_ship_qty'] == 10
    assert wanted['groups'][0]['missing_update_rows'] == 1
    assert all(reference['compare'](result(order_facts), wanted).values())


@pytest.mark.parametrize('defect', ['duplicate_header', 'conflicting_header', 'duplicate_detail', 'missing_quantity',
                                   'missing_sku', 'missing_id', 'missing_created', 'header_delete', 'detail_delete'])
def test_reference_independently_rejects_order_quality_defects(order_facts, reference, defect):
    header(order_facts, created=None if defect == 'missing_created' else '2026-09-01 00:00:00',
           deleted=None if defect == 'header_delete' else 'false')
    detail(order_facts, sku=None if defect == 'missing_sku' else '001', identity=None if defect == 'missing_id' else 'D1',
           quantity=None if defect == 'missing_quantity' else 10, deleted='?' if defect == 'detail_delete' else 'false')
    if defect == 'duplicate_header': header(order_facts)
    if defect == 'conflicting_header': header(order_facts, created='2026-08-01 00:00:00')
    if defect == 'duplicate_detail': detail(order_facts)
    zone = ZoneInfo('Asia/Shanghai')
    plan = OrderComponentPlan(start=datetime(2026, 9, 1, tzinfo=zone), end=datetime(2026, 10, 1, tzinfo=zone))
    with pytest.raises(ValueError): expected(reference, order_facts, plan)
    with pytest.raises(SemanticRuntimeError): result(order_facts, plan)


def test_date_sku_and_owner_scopes_do_not_prorate_order_money(order_facts, reference):
    header(order_facts)
    detail(order_facts)
    detail(order_facts, identity='D2', sku='002', payment=40)
    header(order_facts, order='END', created='2026-10-01 00:00:00')
    header(order_facts, owner='B', deleted='?')
    zone = ZoneInfo('Asia/Shanghai')
    plan = OrderComponentPlan(owner_codes=('A',), sku_codes=('001',),
                              start=datetime(2026, 9, 1, tzinfo=zone), end=datetime(2026, 10, 1, tzinfo=zone))
    wanted = expected(reference, order_facts, plan)
    assert len(wanted['orders']) == 1 and wanted['orders'][0]['header_payment'] == 100
    assert wanted['groups'][0]['detail_payment'] == 60
    assert all(reference['compare'](result(order_facts, plan), wanted).values())


@pytest.mark.parametrize('field', ['order_ship_qty', 'detail_payment', 'known_detail_payment', 'missing_payment_rows',
                                  'missing_update_rows', 'negative_ship_rows', 'currency_code', 'order_created', 'sku_code'])
def test_reference_detects_component_mutation(order_facts, reference, field):
    header(order_facts)
    detail(order_facts)
    checked = result(order_facts)
    checked.groups[0][field] = 'wrong'
    assert not reference['compare'](checked, expected(reference, order_facts))['groups']


def test_reference_uses_exact_rationals_not_ambient_decimal_precision(order_facts, reference):
    header(order_facts)
    detail(order_facts)
    headers, details = raw(reference, order_facts)
    details[0]['total_pay_amount'] = '999999999999999999.01'
    details.append(dict(details[0], id='D2', total_pay_amount='0.02'))
    with localcontext() as context:
        context.prec = 6
        wanted = reference['expected_orders'](headers, details, OrderComponentPlan())
        assert wanted['groups'][0]['detail_payment'] == Fraction(Decimal('999999999999999999.03'))
        assert context.prec == 6


def test_reference_does_not_call_runtime_compilers_or_composer(order_facts, return_facts, reference, monkeypatch):
    import app.agent_core.order_components as order_runtime
    import app.agent_core.return_components as return_runtime
    import app.agent_core.order_return_composition as composition
    header(order_facts)
    detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts)
    def forbidden(*args, **kwargs): raise AssertionError('runtime used by independent expectation')
    monkeypatch.setattr(order_runtime, 'compile_order_components', forbidden)
    monkeypatch.setattr(return_runtime, 'compile_return_components', forbidden)
    monkeypatch.setattr(composition, 'compose_order_returns', forbidden)
    snapshot = reference['reference_snapshot'](OrderComponentPlan(), query_both(order_facts, return_facts), amount_mode='proportional')
    assert snapshot['composition']['groups'][0]['proportional_payment'] == 42


@pytest.mark.parametrize('quantity,returned,payment,mode', [
    (10, 3, 60, 'proportional'), (0, 3, 20, 'proportional'), (2, 3, 10, 'proportional'),
    (-2, 1, 10, 'proportional'), (3, 1, 1, 'proportional'), (10, 3, None, 'proportional'), (10, 3, 60, 'pending'),
])
def test_independent_full_verifier_edge_cases(order_facts, return_facts, reference, quantity, returned, payment, mode):
    header(order_facts)
    detail(order_facts, quantity=quantity, payment=payment)
    return_header(return_facts)
    return_detail(return_facts, quantity=returned)
    checks = reference['verify'](OrderComponentPlan(), query_both(order_facts, return_facts), amount_mode=mode, page_size=2)
    assert checks and all(checks.values())


def test_cancelled_return_does_not_remove_sale_or_fabricate_deduction(order_facts, return_facts, reference):
    header(order_facts, status=901)
    detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts, status=1)
    snapshot = reference['reference_snapshot'](OrderComponentPlan(), query_both(order_facts, return_facts), amount_mode='proportional')
    assert snapshot['composition']['groups'][0]['returned_qty'] == 0
    assert snapshot['composition']['groups'][0]['proportional_payment'] == 60


def test_exact_cohort_unmatched_return_and_empty_scope(order_facts, return_facts, reference):
    header(order_facts, owner='A', order='S1')
    header(order_facts, owner='B', order='S2')
    for owner in ('A', 'B'):
        for order in ('S1', 'S2'):
            return_header(return_facts, owner=owner, order=order, code=order)
            return_detail(return_facts, owner=owner, order=order, code=order, identity=order)
    query = query_both(order_facts, return_facts)
    snapshot = reference['reference_snapshot'](OrderComponentPlan(), query)
    assert {(r['cargo_owner'], r['sales_order_code']) for r in snapshot['composition']['groups']} == {('A', 'S1'), ('B', 'S2')}
    assert all(r['net_qty'] is None for r in snapshot['composition']['groups'])
    assert all(reference['verify'](OrderComponentPlan(), query, page_size=2).values())
    assert all(reference['verify'](OrderComponentPlan(owner_codes=('NONE',)), query, page_size=2).values())


def test_verifier_reports_reference_changes_not_success(order_facts, return_facts, reference):
    header(order_facts)
    detail(order_facts)
    changed = False
    def query(sql, **kwargs):
        nonlocal changed
        if 'header_raw' in sql and not changed:
            changed = True
            order_facts.execute('UPDATE ywzt_ods.scm_sales_order_detail SET ship_qty=11')
        return query_both(order_facts, return_facts)(sql)
    checks = reference['verify'](OrderComponentPlan(), query)
    assert not checks['reference_stable'] and not checks['sales_groups'] and not checks['net_groups']


@pytest.mark.parametrize('source', ['sales_header', 'sales_detail', 'return_header', 'return_detail'])
def test_failed_raw_read_retains_exact_stage_without_data(order_facts, return_facts, reference, source):
    header(order_facts)
    detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts)
    target = {'sales_header': 'scm_sales_order_header', 'sales_detail': 'scm_sales_order_detail',
              'return_header': 'scm_return_order_header', 'return_detail': 'scm_return_order_detail'}[source]
    def query(sql, **kwargs):
        raw_reads = [select for select in sqlglot.parse_one(sql, read='doris').find_all(exp.Select)
                     if any(field.alias_or_name == 'group_count' for field in select.selects)
                     and isinstance(select.args.get('from_'), exp.From)
                     and isinstance(select.args['from_'].this, exp.Table)
                     and select.args['from_'].this.name == target]
        if raw_reads:
            raise SemanticRuntimeError('private source detail', reason='result_truncated')
        return query_both(order_facts, return_facts)(sql)
    with pytest.raises(Exception) as failure:
        reference['verify'](OrderComponentPlan(), query)
    error = failure.value
    assert error.stage == 'reference_before.' + source
    assert error.reason == 'result_truncated'
    assert 'private source detail' not in str(error)


def test_return_conflict_is_reported_before_raw_return_paging(order_facts, return_facts, reference):
    header(order_facts)
    detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts, order='OTHER')
    def query(sql, **kwargs):
        if 'scm_return_' in sql:
            assert 'reference_return_quality' in sql, 'raw returns read before known conflict'
        return query_both(order_facts, return_facts)(sql)
    with pytest.raises(reference['VerificationFailure']) as failure:
        reference['reference_snapshot'](OrderComponentPlan(), query)
    error = failure.value
    assert error.stage == 'reference.return_preflight'
    assert error.reason == 'return_reference_quality_failed'
    assert error.quality_flags == ('conflicting_detail_reference',)
