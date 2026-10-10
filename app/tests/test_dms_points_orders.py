from copy import deepcopy
from datetime import date
import sqlite3

import pytest
import sqlglot

from app.agent_core.dms_points_orders import DmsPointsOrderPlan, compile_dms_points_orders, reconcile_dms_points_orders, retrieve_dms_points_orders
from app.agent_core.semantic_types import SemanticRuntimeError


@pytest.fixture
def orders():
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS dms_ods")
    db.execute('''CREATE TABLE dms_ods.t_sales_order (
        sales_order_code TEXT, customer_code TEXT, customer_name TEXT, created_time TEXT,
        updated_time TEXT, version INTEGER, id INTEGER, is_points_order INTEGER,
        deleted_flag INTEGER, _fdl_marked_deleted TEXT,
        paid_status TEXT, order_status TEXT, order_type TEXT)''')
    yield db
    db.close()


def add(db, code='O1', customer='001', name='Test customer', created='2026-01-01 00:00:00',
        updated='2026-09-01 12:00:00', version=1, row_id=1, points=1, deleted=0, cdc='false'):
    db.execute('INSERT INTO dms_ods.t_sales_order VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (code,customer,name,created,updated,version,row_id,points,deleted,cdc,'0','100','OTHER'))


def plan(**kwargs):
    return DmsPointsOrderPlan(start=date(2026,1,1), end=date(2026,9,1), **kwargs)


def query(db, sql):
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


def run(db, request=None):
    request = request or plan()
    return reconcile_dms_points_orders(query(db,compile_dms_points_orders(request)),request)


def test_unpaid_non_so16_no_detail_orders_remain_and_months_reconcile(orders):
    add(orders)
    add(orders)
    add(orders,code='O2',customer=None,name=None,created='2026-08-31 23:59:59',row_id=2)
    add(orders,code='O3',created='2026-09-01 00:00:00',row_id=3)
    add(orders,code='O4',created='2025-12-31 23:59:59',row_id=4)
    add(orders,code='O5',points=0,row_id=5)
    result = run(orders)
    assert result.order_count == 2
    assert [(r['sales_order_code'],r['customer_code']) for r in result.orders] == [('O1','001'),('O2',None)]
    assert result.months == [{'month':'2026-01','order_count':1},{'month':'2026-08','order_count':1}]
    assert result.missing_customer_code_count == result.missing_customer_name_count == 1


@pytest.mark.parametrize('change', [{'deleted':1},{'cdc':' TRUE '},{'cdc':'1'},{'points':0},{'created':'2026-09-01 00:00:00'},{'customer':'002'}])
def test_resolve_current_version_before_business_filters(orders,change):
    add(orders)
    add(orders,updated='2026-09-02 00:00:00',version=2,row_id=2,**change)
    request = plan(customer_codes=('001',)) if 'customer' in change else plan()
    assert run(orders,request).order_count == 0


@pytest.mark.parametrize('changes', [{'name':'Conflict'},{'deleted':1},{'points':0},{'created':'2026-09-01 00:00:00'}])
def test_equal_rank_conflict_never_arbitrarily_selects_an_answer(orders,changes):
    add(orders)
    add(orders,**changes)
    with pytest.raises(ValueError,match='quality'):
        run(orders)


@pytest.mark.parametrize('changes', [{'code':''},{'code':'   '},{'code':None},{'created':None},{'cdc':'unknown'}])
def test_invalid_source_not_hidden_as_empty_success(orders,changes):
    add(orders,**changes)
    with pytest.raises(ValueError,match='quality'):
        run(orders)


def test_unrelated_scope_anomaly_does_not_poison_exact_customer(orders):
    add(orders,customer='001')
    add(orders,code=None,customer='002',created=None,row_id=2)
    assert run(orders,plan(customer_codes=('001',))).order_count == 1


def test_null_update_is_diagnosed_not_invented_as_fresh(orders):
    add(orders,updated=None)
    result=run(orders)
    assert result.source_latest_updated is None and result.missing_updated_count == 1


def test_empty_report_retains_controls(orders):
    rows=query(orders,compile_dms_points_orders(plan()))
    assert len(rows)==1 and rows[0]['row_kind']=='control'
    result=reconcile_dms_points_orders(rows,plan())
    assert result.order_count==0 and result.orders==[] and result.months==[]


def test_all_dates_and_exact_customer_order_scopes(orders):
    add(orders,code='o-A',customer='001',created='2020-01-01 00:00:00')
    add(orders,code='O-A',customer='1',row_id=2)
    result=run(orders,DmsPointsOrderPlan(customer_codes=('001',),order_codes=('o-A',)))
    assert [r['sales_order_code'] for r in result.orders]==['o-A']


@pytest.mark.parametrize('change', ['missing_order','duplicate_order','monthly_count','total_count','created_month','customer_count','updated_count','latest_time'])
def test_reconciliation_rejects_corrupt_results(orders,change):
    add(orders)
    rows=query(orders,compile_dms_points_orders(plan()))
    order=next(r for r in rows if r['row_kind']=='order')
    month=next(r for r in rows if r['row_kind']=='month')
    if change=='missing_order': rows.remove(order)
    if change=='duplicate_order': rows.append(deepcopy(order))
    if change=='monthly_count': month['order_count']=2
    if change=='total_count': rows[0]['total_order_count']=2
    if change=='created_month': order['month']='2026-02'
    if change=='customer_count':
        for r in rows:r['missing_customer_code_count']=1
    if change=='updated_count':
        for r in rows:r['missing_updated_count']=1
    if change=='latest_time':
        for r in rows:r['source_latest_updated']='2020-01-01 00:00:00'
    with pytest.raises(ValueError):reconcile_dms_points_orders(rows,plan())


def test_complete_pagination_and_capacity(orders):
    for i in range(7):add(orders,code=f'O{i}',row_id=i+1)
    calls=[]
    def read(sql,**kwargs):
        calls.append(sql)
        return query(orders,sql)
    result=retrieve_dms_points_orders(plan(),read,page_size=3)
    assert result.order_count==7 and len(calls)==6
    with pytest.raises(SemanticRuntimeError) as exc:
        retrieve_dms_points_orders(plan(),read,page_size=3,max_rows=8)
    assert exc.value.reason=='result_truncated'


def test_complete_batches_preserve_month_order_and_quality_controls(orders):
    for i in range(7):add(orders,code=f'O{i}',row_id=i+1)
    waves=[]
    def read(sql,**kwargs):return query(orders,sql)
    def many(statements,**kwargs):
        waves.append(len(statements))
        return [read(sql) for sql in statements]
    actual=retrieve_dms_points_orders(plan(),read,query_many=many,page_size=1)
    assert actual==run(orders) and waves==[1,4,4]*2
    def invalid(*args,**kwargs):
        batches=many(*args,**kwargs)
        if len(batches)>1:batches[-1][0]['invalid_delete_count']=1
        return batches
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_dms_points_orders(plan(),read,query_many=invalid,page_size=1)
    assert failure.value.reason=='dms_points_order_quality_failed'


@pytest.mark.parametrize('bad', [{'start':date(2026,1,1)}, {'start':date(2026,9,1),'end':date(2026,1,1)}, {'customer_codes':'001'}, {'order_codes':('',)}])
def test_invalid_scope_is_not_silently_all(bad):
    with pytest.raises(ValueError):DmsPointsOrderPlan(**bad)


def test_independent_reference_before_live(orders):
    from importlib.util import module_from_spec,spec_from_file_location
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'tests/acceptance/check_dms_points_orders_live.py'
    spec=spec_from_file_location('dms_reference',path)
    module=module_from_spec(spec)
    spec.loader.exec_module(module)
    add(orders)
    add(orders)
    add(orders,code='O2',name=None,created='2026-08-31 23:59:59',row_id=2)
    add(orders,code='O3',deleted=1,row_id=3)
    add(orders,code='O4',points=0,row_id=4)
    assert all(module.compare(run(orders),query(orders,module.reference_sql())).values())
    reference = query(orders,module.reference_sql())
    reference[0]['created_time'] = reference[0]['created_time'].replace(' ', 'T') + '+08:00'
    assert all(module.compare(run(orders),reference).values())
    reference[0]['customer_name'] = 'Different customer'
    assert not module.compare(run(orders),reference)['order_details']


@pytest.mark.parametrize('corrupt',['missing_page','changed_customer','missing_control'])
def test_page_corruption_and_same_count_source_change_fail(orders,corrupt):
    add(orders)
    calls=0
    def read(sql,**kwargs):
        nonlocal calls
        calls+=1
        rows=query(orders,sql)
        if corrupt=='missing_page' and calls==2:return []
        if corrupt=='changed_customer' and calls==6:rows[0]['customer_name']='Changed'
        if corrupt=='missing_control':rows[0].pop('total_order_count',None)
        return rows
    with pytest.raises(SemanticRuntimeError):retrieve_dms_points_orders(plan(),read,page_size=1)
