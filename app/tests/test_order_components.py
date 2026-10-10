from datetime import datetime
from decimal import Decimal
import sqlite3
from zoneinfo import ZoneInfo

import pytest
import sqlglot

from app.agent_core.order_components import OrderComponentPlan, compile_order_components, reconcile_order_components
from app.agent_core.semantic_types import SemanticRuntimeError


@pytest.fixture
def facts():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.execute('CREATE TABLE ywzt_ods.scm_sales_order_header (company_code TEXT,sales_order_code TEXT,currency_code TEXT,total_pay_amount NUMERIC,created TEXT,updated TEXT,status INT,_fdl_marked_deleted TEXT)')
    db.execute('CREATE TABLE ywzt_ods.scm_sales_order_detail (id TEXT,company_code TEXT,sales_order_code TEXT,sku_code TEXT,ship_qty INT,total_pay_amount NUMERIC,status INT,updated TEXT,_fdl_marked_deleted TEXT)')
    yield db
    db.close()


def header(db, owner='A', order='S1', currency='CNY', payment=100, created='2026-09-01 00:00:00', deleted='false', status=100):
    db.execute('INSERT INTO ywzt_ods.scm_sales_order_header VALUES (?,?,?,?,?,?,?,?)',
               (owner, order, currency, payment, created, '2026-09-11 10:00:00', status, deleted))


def detail(db, identity='D1', owner='A', order='S1', sku='001', quantity=10, payment=60, status=0, deleted='false'):
    db.execute('INSERT INTO ywzt_ods.scm_sales_order_detail VALUES (?,?,?,?,?,?,?,?,?)',
               (identity, owner, order, sku, quantity, payment, status, '2026-09-11 09:00:00', deleted))


def read(db, sql, **kwargs):
    return [dict(row) for row in db.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]


def result(db, plan=None):
    plan = plan or OrderComponentPlan()
    return reconcile_order_components(read(db, compile_order_components(plan)), plan)


def test_sales_lines_group_before_returns_and_header_money_once(facts):
    header(facts)
    detail(facts, quantity=6, payment=36)
    detail(facts, identity='D2', quantity=4, payment=24)
    detail(facts, identity='D3', sku='002', quantity=5, payment=40)
    checked = result(facts)
    assert len(checked.orders) == 1 and len(checked.groups) == 2
    assert checked.groups[0]['order_ship_qty'] == 10
    assert checked.groups[0]['detail_payment'] == 60
    assert checked.orders[0]['header_payment'] == 100
    assert checked.currency_totals['CNY']['header_payment'] == 100
    assert checked.currency_totals['CNY']['detail_payment'] == 100
    assert checked.order_keys == (('A', 'S1'),)


def test_currencies_and_owner_order_identity_are_never_merged(facts):
    header(facts)
    header(facts, owner='B', currency='USD', payment=200)
    detail(facts)
    detail(facts, owner='B', payment=200)
    checked = result(facts)
    assert len(checked.orders) == len(checked.groups) == 2
    assert checked.currency_totals['CNY']['header_payment'] == 100
    assert checked.currency_totals['USD']['header_payment'] == 200


def test_header_without_active_detail_survives(facts):
    header(facts)
    detail(facts, status=1)
    checked = result(facts)
    assert len(checked.orders) == 1 and checked.groups == []
    assert checked.orders[0]['missing_details']


def test_time_scope_is_header_creation_half_open_and_timezone_aware(facts):
    header(facts, order='BEFORE', created='2026-08-31 23:59:59')
    header(facts)
    header(facts, order='END', created='2026-09-10 00:00:00')
    zone = ZoneInfo('Asia/Shanghai')
    scoped = OrderComponentPlan(start=datetime(2026, 9, 1, tzinfo=zone), end=datetime(2026, 9, 10, tzinfo=zone))
    assert result(facts, scoped).order_keys == (('A', 'S1'),)
    assert len(result(facts).orders) == 3


def test_sku_scope_keeps_whole_order_money_separate(facts):
    header(facts)
    detail(facts)
    detail(facts, identity='D2', sku='002', payment=40)
    checked = result(facts, OrderComponentPlan(sku_codes=('001',)))
    assert len(checked.groups) == 1
    assert checked.currency_totals['CNY']['detail_payment'] == 60
    assert checked.currency_totals['CNY']['header_payment'] == 100
    assert result(facts, OrderComponentPlan(owner_codes=('OTHER',))).orders == []


def test_missing_money_and_currency_are_not_fabricated(facts):
    header(facts, payment=None, currency=None)
    detail(facts, payment=None)
    checked = result(facts)
    assert checked.orders[0]['header_payment'] is None
    assert checked.groups[0]['detail_payment'] is None
    assert checked.groups[0]['currency_code'] is None
    assert checked.currency_totals[None]['missing_detail_payment_rows'] == 1


def test_zero_and_negative_ship_values_are_preserved(facts):
    header(facts)
    detail(facts, quantity=-1)
    detail(facts, identity='D2', quantity=2)
    detail(facts, identity='D3', sku='ZERO', quantity=0)
    checked = result(facts)
    assert checked.groups[0]['order_ship_qty'] == 1
    assert checked.groups[0]['negative_ship_rows'] == 1
    assert checked.groups[1]['order_ship_qty'] == 0


@pytest.mark.parametrize('defect', ['header_duplicate', 'header_conflict', 'detail_duplicate', 'missing_quantity', 'missing_id', 'missing_sku', 'header_delete', 'detail_delete', 'missing_created'])
def test_invalid_sources_cannot_become_trusted_components(facts, defect):
    header(facts, created=None if defect == 'missing_created' else '2026-09-01 00:00:00', deleted=None if defect == 'header_delete' else 'false')
    detail(facts, identity=None if defect == 'missing_id' else 'D1', sku=None if defect == 'missing_sku' else '001',
           quantity=None if defect == 'missing_quantity' else 10, deleted='?' if defect == 'detail_delete' else 'false')
    if defect == 'header_duplicate': header(facts)
    if defect == 'header_conflict': header(facts, currency='USD')
    if defect == 'detail_duplicate': detail(facts)
    with pytest.raises(SemanticRuntimeError): result(facts)


def test_logical_delete_and_detail_status_do_not_filter_other_facts(facts):
    header(facts, status=901)
    detail(facts)
    detail(facts, identity='DELETED', deleted='true')
    header(facts, order='DELETED', deleted='true')
    assert result(facts).groups[0]['source_rows'] == 1


@pytest.mark.parametrize('field', ['order_ship_qty', 'detail_payment', 'source_rows', 'negative_ship_rows', 'missing_payment_rows'])
def test_prejoin_controls_reject_mutated_groups(facts, field):
    header(facts)
    detail(facts)
    plan = OrderComponentPlan()
    rows = read(facts, compile_order_components(plan))
    row = next(r for r in rows if r['row_kind'] == 'sku')
    row[field] += 1
    with pytest.raises(SemanticRuntimeError): reconcile_order_components(rows, plan)


@pytest.mark.parametrize('kwargs', [{'owner_codes': 'A'}, {'order_codes': ('',)}, {'start': datetime(2026,9,1)}, {'sku_codes': (' 1',)}])
def test_invalid_scopes_rejected(kwargs):
    with pytest.raises(ValueError): OrderComponentPlan(**kwargs)


@pytest.mark.parametrize('kind,field,value', [
    ('order', 'detail_payment', 999), ('sku', 'header_payment', 999),
    ('header_control', 'order_ship_qty', 999), ('detail_control', 'header_payment', 999),
    ('header_control', 'negative_ship_rows', 1),
])
def test_irrelevant_values_cannot_cross_money_grains(facts, kind, field, value):
    header(facts)
    detail(facts)
    rows = read(facts, compile_order_components(OrderComponentPlan()))
    next(row for row in rows if row['row_kind'] == kind)[field] = value
    with pytest.raises(SemanticRuntimeError): reconcile_order_components(rows, OrderComponentPlan())


def test_partial_currency_money_is_explicitly_known_only(facts):
    header(facts, payment=None)
    detail(facts, payment=None)
    detail(facts, identity='D2', payment=25)
    checked = result(facts)
    assert checked.currency_totals['CNY']['header_payment'] is None
    assert checked.currency_totals['CNY']['detail_payment'] is None
    assert checked.currency_totals['CNY']['known_detail_payment'] == 25
    assert checked.groups[0]['detail_payment'] is None


def test_missing_update_counts_survive_nonmissing_group_maximum(facts):
    header(facts)
    detail(facts)
    detail(facts, identity='D2')
    facts.execute('UPDATE ywzt_ods.scm_sales_order_header SET updated=NULL')
    facts.execute("UPDATE ywzt_ods.scm_sales_order_detail SET updated=NULL WHERE id='D2'")
    checked = result(facts)
    assert checked.orders[0]['missing_update_rows'] == 1
    assert checked.groups[0]['missing_update_rows'] == 1
    assert checked.groups[0]['latest_update'] is not None
    assert checked.currency_totals['CNY']['missing_header_update_rows'] == 1
    assert checked.currency_totals['CNY']['missing_detail_update_rows'] == 1


@pytest.mark.parametrize('kind', ['order', 'sku', 'header_control', 'detail_control'])
def test_missing_update_count_corruption_rejected(facts, kind):
    header(facts)
    detail(facts)
    rows = read(facts, compile_order_components(OrderComponentPlan()))
    next(r for r in rows if r['row_kind'] == kind)['missing_update_rows'] = 1
    with pytest.raises(SemanticRuntimeError): reconcile_order_components(rows, OrderComponentPlan())
