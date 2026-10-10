from copy import deepcopy
from datetime import date
import re
import sqlite3

import pytest
import sqlglot

from app.agent_core.dms_manager_orders import DmsManagerOrderPlan, compile_manager_orders, reconcile_manager_orders, retrieve_manager_orders


@pytest.fixture
def facts():
    db=sqlite3.connect(':memory:',check_same_thread=False)
    db.row_factory=sqlite3.Row
    db.create_function('regexp_like',2,lambda value,pattern:bool(re.search(pattern,value or '')))
    db.create_function('regexp_replace',3,lambda value,pattern,replacement:re.sub(pattern,replacement,value) if value is not None else None)
    db.execute("ATTACH DATABASE ':memory:' AS dms_ods")
    db.execute('CREATE TABLE dms_ods.t_sales_order(sales_order_code TEXT,customer_code TEXT,customer_name TEXT,created_time TEXT,updated_time TEXT,version INT,id INT,deleted_flag INT,_fdl_marked_deleted TEXT,owner_manager TEXT,province_department_name TEXT,is_points_order INT,paid_status TEXT)')
    db.execute('CREATE TABLE dms_ods.t_employee(employee_id INTEGER,actual_name TEXT,deleted_flag INT,update_time TEXT)')
    yield db
    db.close()


def add(db,code='O1',customer='001',name='Order customer',created='2026-09-01 00:00:00',updated='2026-09-10 00:00:00',version=1,row_id=1,deleted=0,cdc='false',manager='001',province='Test Province',points=0):
    db.execute('INSERT INTO dms_ods.t_sales_order VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(code,customer,name,created,updated,version,row_id,deleted,cdc,manager,province,points,'0'))


def employee(db,key=1,name='Manager',deleted=0,updated='2026-09-10 00:00:00'):
    db.execute('INSERT INTO dms_ods.t_employee VALUES(?,?,?,?)',(key,name,deleted,updated))


def query(db,sql):
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


def result(db,plan=None):
    plan=plan or DmsManagerOrderPlan()
    return reconcile_manager_orders(query(db,compile_manager_orders(plan)),plan)


def test_all_orders_no_points_payment_or_detail_requirement(facts):
    employee(facts)
    employee(facts)
    add(facts)
    add(facts)
    add(facts,code='O2',manager='+1',points=1,row_id=2)
    actual=result(facts)
    assert actual.order_count==2 and len(actual.groups)==1
    assert actual.groups[0]['order_count']==2
    assert actual.groups[0]['customer_name']=='Order customer'
    assert actual.groups[0]['manager_name']=='Manager'


def test_missing_manager_count_is_orders_not_reference_count(facts):
    for i in range(3):add(facts,code=f'O{i}',manager='99',row_id=i)
    add(facts,code='O4',manager=None,row_id=4)
    actual=result(facts)
    assert actual.order_count==actual.missing_manager_order_count==4
    assert sum(row['missing_manager_order_count'] for row in actual.groups)==4
    assert all(row['manager_name'] is None for row in actual.groups)


def test_employee_conflicts_deleted_missing_and_invalid_keep_orders(facts):
    employee(facts,key=1,name='A')
    employee(facts,key=1,name='B')
    employee(facts,key=2,deleted=1)
    employee(facts,key=3,name=None)
    for i,manager in enumerate(['1','2','3','4','bad',None]):add(facts,code=f'O{i}',manager=manager,row_id=i)
    actual=result(facts)
    assert actual.order_count==actual.missing_manager_order_count==6
    assert {row['manager_state'] for row in actual.groups}=={'conflicting_employee','deleted_employee','missing_name','not_found','invalid_reference','missing_reference'}


@pytest.mark.parametrize('change',[{'deleted':1},{'cdc':'true'},{'created':'2026-09-10 00:00:00'},{'customer':'002'}])
def test_order_current_version_before_scope(facts,change):
    add(facts)
    add(facts,updated='2026-09-11 00:00:00',version=2,row_id=2,**change)
    plan=DmsManagerOrderPlan(start=date(2026,9,1),end=date(2026,9,10),customer_codes=('001',))
    assert result(facts,plan).order_count==0


@pytest.mark.parametrize('change',[{'manager':'2'},{'province':'Other'},{'name':'Other'},{'deleted':1},{'created':'2026-09-11 00:00:00'}])
def test_conflicting_current_order_never_arbitrarily_selected(facts,change):
    add(facts)
    add(facts,**change)
    with pytest.raises(ValueError,match='quality'):result(facts)


@pytest.mark.parametrize('change',[{'code':None},{'code':' '},{'created':None},{'cdc':'unknown'},{'deleted':2}])
def test_invalid_source_does_not_become_empty_success(facts,change):
    add(facts,**change)
    with pytest.raises(ValueError,match='quality'):result(facts)


def test_native_province_and_historical_customer_names_do_not_merge(facts):
    employee(facts)
    add(facts)
    add(facts,code='O2',name='Historical name',row_id=2)
    add(facts,code='O3',customer=None,name=None,province=None,row_id=3)
    actual=result(facts)
    assert len(actual.groups)==3 and actual.missing_province_order_count==1
    assert actual.missing_customer_code_count==actual.missing_customer_name_count==1


def test_creation_window_and_all_scope_are_request_local(facts):
    add(facts,created='2020-01-01 00:00:00')
    add(facts,code='O2',created='2026-09-09 23:59:59',row_id=2)
    add(facts,code='O3',created='2026-09-10 00:00:00',row_id=3)
    assert result(facts,DmsManagerOrderPlan(start=date(2026,9,1),end=date(2026,9,10))).order_count==1
    assert result(facts).order_count==3


def test_exact_code_scope_preserves_case_and_leading_zeros(facts):
    add(facts,code='o-a')
    add(facts,code='O-A',customer='1',row_id=2)
    assert result(facts,DmsManagerOrderPlan(customer_codes=('001',),order_codes=('o-a',))).order_count==1


@pytest.mark.parametrize('bad',['missing','duplicate','total','group','missing_manager','manager_name','customer_name','date','freshness'])
def test_control_reconciliation_detects_corruption(facts,bad):
    employee(facts)
    add(facts)
    rows=query(facts,compile_manager_orders(DmsManagerOrderPlan()))
    order=next(row for row in rows if row['row_kind']=='order')
    if bad=='missing':rows.remove(order)
    if bad=='duplicate':rows.append(deepcopy(order))
    if bad=='total':rows[0]['total_order_count']=2
    if bad=='group':order['group_order_count']=2
    if bad=='missing_manager':rows[0]['missing_manager_order_count']=1
    if bad=='manager_name':order['manager_name']=None
    if bad=='customer_name':order['customer_name']=None
    if bad=='date':order['order_date']='2020-01-01'
    if bad=='freshness':order['updated_time']='2000-01-01'
    with pytest.raises(ValueError):reconcile_manager_orders(rows,DmsManagerOrderPlan())


def test_empty_and_bounded_complete_pages(facts):
    assert result(facts).order_count==0
    for i in range(6):add(facts,code=f'O{i}',row_id=i,manager=str(i))
    calls=[]
    def execute(sql,*,purpose):
        calls.append(sql)
        return query(facts,sql)
    actual=retrieve_manager_orders(DmsManagerOrderPlan(),execute,page_size=2)
    assert actual.order_count==6 and len(calls)==8


def test_all_manager_references_are_not_limited_to_literal_lookup_budget(facts):
    for i in range(205):
        add(facts,code=f'O{i}',row_id=i,manager=str(i))
        employee(facts,key=i,name=f'Manager {i}')
    actual=result(facts)
    assert actual.order_count==205 and len(actual.groups)==205
    assert actual.missing_manager_order_count==0


def test_unrelated_blank_identity_and_customer_scope(facts):
    add(facts)
    add(facts,code=None,customer='002',created=None)
    assert result(facts,DmsManagerOrderPlan(customer_codes=('001',))).order_count==1


def test_missing_employee_timestamp_cannot_be_claimed_current(facts):
    add(facts)
    employee(facts,updated=None)
    with pytest.raises(ValueError,match='timestamp'):result(facts)


@pytest.mark.parametrize('bad',[{'start':date(2026,9,1)},{'start':date(2026,9,10),'end':date(2026,9,1)},{'order_codes':('x','x')},{'customer_codes':'001'}])
def test_invalid_plan_is_not_silently_all(bad):
    with pytest.raises(ValueError):DmsManagerOrderPlan(**bad)


@pytest.mark.parametrize('corrupt',['missing_page','changed_name','missing_control','capacity'])
def test_paging_failure_is_not_success(facts,corrupt):
    from app.agent_core.semantic_types import SemanticRuntimeError
    add(facts)
    calls=0
    def execute(sql,*,purpose):
        nonlocal calls
        calls+=1
        rows=query(facts,sql)
        if corrupt=='missing_page' and calls==2:return []
        if corrupt=='changed_name' and calls==4:rows[0]['customer_name']='Changed'
        if corrupt=='missing_control':rows[0].pop('total_order_count')
        return rows
    with pytest.raises(SemanticRuntimeError):
        retrieve_manager_orders(DmsManagerOrderPlan(),execute,page_size=1,max_rows=1 if corrupt=='capacity' else 100000)


def test_independent_reference_before_live(facts):
    from importlib.util import module_from_spec,spec_from_file_location
    from pathlib import Path
    spec=spec_from_file_location('order_reference',Path(__file__).resolve().parents[2]/'tests/acceptance/check_dms_manager_orders_live.py')
    reference=module_from_spec(spec)
    spec.loader.exec_module(reference)
    employee(facts)
    employee(facts)
    add(facts)
    add(facts)
    add(facts,code='O2',manager='99',name=None,row_id=2)
    add(facts,code='O3',manager=None,province=None,row_id=3)
    add(facts,code='O4',customer='1',deleted=1,row_id=4)
    add(facts,code='O5',created='2020-01-01 00:00:00',row_id=5)
    plan=DmsManagerOrderPlan()
    headers=reference.reference_headers(query(facts,reference.reference_sql(plan)),plan)
    refs=tuple(dict.fromkeys(row['manager_reference'] for row in headers))
    employees=query(facts,reference.manager_reference.reference_sql(refs))
    expected=reference.expected_report(headers,employees)
    assert all(reference.compare(result(facts,plan),expected).values())
    expected['groups'][0]['order_count']+=1
    assert not reference.compare(result(facts,plan),expected)['all_group_keys_labels_and_counts']


@pytest.mark.parametrize('change',[{'manager':'2'},{'deleted':1},{'name':None},{'created':'2026-09-11 00:00:00'}])
def test_independent_reference_rejects_same_rank_conflicts(facts,change):
    from importlib.util import module_from_spec,spec_from_file_location
    from pathlib import Path
    spec=spec_from_file_location('order_reference',Path(__file__).resolve().parents[2]/'tests/acceptance/check_dms_manager_orders_live.py')
    reference=module_from_spec(spec)
    spec.loader.exec_module(reference)
    add(facts)
    add(facts,**change)
    plan=DmsManagerOrderPlan()
    with pytest.raises(ValueError,match='conflicting'):
        reference.reference_headers(query(facts,reference.reference_sql(plan)),plan)
