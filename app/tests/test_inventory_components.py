from datetime import datetime
from decimal import Decimal
import re
import sqlite3
from zoneinfo import ZoneInfo

import pytest
import sqlglot

from app.agent_core.inventory_components import InventoryComponentPlan, compile_inventory_components, reconcile_inventory_components, retrieve_inventory_components
from app.agent_core.semantic_types import SemanticRuntimeError


@pytest.fixture
def facts():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.create_function('REGEXP',2,lambda pattern,value: bool(re.search(pattern,value or '')))
    db.create_function('REGEXP_LIKE',2,lambda value,pattern: bool(re.search(pattern,value or '')))
    db.create_function('GREATEST',2,lambda left,right: max(left,right) if left is not None and right is not None else None)
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.execute('CREATE TABLE ywzt_ods.scm_warehous_manage (cargo_owner_id TEXT,wms_code TEXT,sku_code TEXT,in_stock_quantity NUMERIC,stock_lock TEXT,freeze_quantity NUMERIC,move_in_quantity NUMERIC,inventory_status TEXT,stock_change_time TEXT)')
    db.execute('CREATE TABLE ywzt_ods.scm_warehous_occupy (cargo_owner_id TEXT,wms_code TEXT,sku_code TEXT,freeze_quantity NUMERIC,pre_order_code TEXT,status TEXT,created_date TEXT)')
    yield db
    db.close()


def plan(**kwargs):
    return InventoryComponentPlan(observed_at=datetime(2026,9,10,12,tzinfo=ZoneInfo('Asia/Shanghai')),
                                  occupation_since=datetime(2026,7,12,12,tzinfo=ZoneInfo('Asia/Shanghai')),**kwargs)


def stock(db,owner='A',warehouse='W',sku='001',quantity=100,lock='0',freeze=7,move=0,quality='ZP',changed='2026-09-10 11:00:00'):
    db.execute('INSERT INTO ywzt_ods.scm_warehous_manage VALUES (?,?,?,?,?,?,?,?,?)',(owner,warehouse,sku,quantity,lock,freeze,move,quality,changed))


def occupy(db,owner='A',warehouse='W',sku='001',quantity=5,reference='ORDER',status='0',created='2026-09-10 10:00:00'):
    db.execute('INSERT INTO ywzt_ods.scm_warehous_occupy VALUES (?,?,?,?,?,?,?)',(owner,warehouse,sku,quantity,reference,status,created))


def query(db,sql):
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


def result(db,request=None):
    request=request or plan()
    return reconcile_inventory_components(query(db,compile_inventory_components(request)),request)


def test_preaggregation_does_not_multiply_stock_or_occupation(facts):
    stock(facts,quantity=50,lock='2',freeze=6)
    stock(facts,quantity=50,lock='3',freeze=7)
    occupy(facts,quantity=10)
    occupy(facts,quantity=15,reference='HJXH-TH001')
    row=result(facts).groups[0]
    assert row['in_stock_ea']==100 and row['sales_occupation_ea']==10 and row['transfer_occupation_ea']==15
    assert row['available_ea']==70 and row['deduction_ea']==5
    assert row['allocation_ea']==10 and row['business_occupation_ea']==25
    assert row['stock_rows']==row['occupation_rows']==2


def test_lock_or_freeze_is_selected_after_key_aggregation(facts):
    stock(facts,lock='5',freeze=40)
    stock(facts,lock='-5',freeze=30)
    row=result(facts).groups[0]
    assert row['deduction_ea']==70 and row['available_ea']==130


def test_owner_warehouse_sku_keys_and_all_reset(facts):
    for owner in ('A','B'):
        for warehouse in ('W','V'):
            for sku in ('001','1'):stock(facts,owner,warehouse,sku)
    occupy(facts,owner='B',quantity=90)
    assert len(result(facts).groups)==8
    assert len(result(facts,plan(owner_codes=('A',),warehouse_codes=('W',),sku_codes=('001',))).groups)==1
    assert len(result(facts).groups)==8


@pytest.mark.parametrize('quality,kept',[('ZP',True),('',True),(None,True),('CP',False),(' ZP ',False)])
def test_quality_filter_does_not_inherit_other_report_rules(facts,quality,kept):
    stock(facts,quality=quality)
    assert bool(result(facts).groups)==kept


def test_explicit_window_and_status_boundaries(facts):
    stock(facts)
    occupy(facts,quantity=30,created='2026-07-12 12:00:00')
    occupy(facts,quantity=20,created='2026-07-12 12:00:01')
    occupy(facts,quantity=40,status='01')
    occupy(facts,quantity=50,status=None)
    occupy(facts,quantity=60,status='2',created='2026-09-11 00:00:00')
    row=result(facts).groups[0]
    assert row['sales_occupation_ea']==80 and row['future_occupation_rows']==1
    assert row['available_ea']==13


def test_null_reference_is_explicit_historical_sales_with_diagnostic(facts):
    stock(facts,owner='OTHER')
    occupy(facts,owner='OTHER',reference=None)
    occupy(facts,owner='OTHER',reference='HJXH-TH123',quantity=10)
    row=result(facts).groups[0]
    assert row['sales_occupation_ea']==5 and row['transfer_occupation_ea']==10
    assert row['missing_reference_rows']==1


def test_negative_components_and_clamping_are_not_hidden(facts):
    stock(facts,quantity=-5,lock='0',freeze=0,move=-2)
    occupy(facts,quantity=-10)
    row=result(facts).groups[0]
    assert row['available_ea']==5 and row['available_above_stock']
    assert row['negative_stock_rows']==row['negative_stock_move_rows']==row['negative_occupation_rows']==1
    stock(facts,sku='CLIPPED',quantity=3,freeze=10)
    clipped=next(row for row in result(facts).groups if row['sku_code']=='CLIPPED')
    assert clipped['available_ea']==0 and clipped['raw_available_ea']==-7 and clipped['clamped']


def test_occupation_only_key_has_no_invented_inventory(facts):
    occupy(facts)
    row=result(facts).groups[0]
    assert row['stock_rows']==0 and row['available_ea'] is None and row['raw_available_ea'] is None
    assert row['sales_occupation_ea']==5


@pytest.mark.parametrize('kind',['lock','stock_key','occupation_key','status','date'])
def test_source_quality_fails_without_silent_zero_or_lost_fact(facts,kind):
    stock(facts,lock='invalid' if kind=='lock' else '0',owner=None if kind=='stock_key' else 'A')
    occupy(facts,sku='' if kind=='occupation_key' else '001',status='bad' if kind=='status' else '0',created=None if kind=='date' else '2026-09-10 10:00:00')
    with pytest.raises(ValueError,match='quality'):result(facts)


def test_exact_fractional_components_and_missing_timestamps(facts):
    stock(facts,quantity=1.125,lock='0.125',freeze=.5,changed=None)
    occupy(facts,quantity=.25)
    row=result(facts).groups[0]
    assert row['available_ea']==Decimal('.750') and row['missing_stock_time_rows']==1


@pytest.mark.parametrize('field',['available_ea','in_stock_ea','total_in_stock_ea','stock_rows','total_stock_rows','deduction_ea','allocation_ea','business_occupation_ea'])
def test_reconciliation_rejects_changed_components_and_controls(facts,field):
    stock(facts)
    occupy(facts)
    rows=query(facts,compile_inventory_components(plan()))
    row=next(row for row in rows if row['row_kind']=='group')
    row[field]+=1
    with pytest.raises(ValueError):reconcile_inventory_components(rows,plan())


def test_empty_complete_pages_and_capacity(facts):
    assert result(facts).groups==[]
    for i in range(8):stock(facts,sku=str(i))
    calls=[]
    def read(sql,**kwargs):
        calls.append(sql)
        return query(facts,sql)
    actual=retrieve_inventory_components(plan(),read,page_size=2)
    assert len(actual.groups)==8 and len(calls)==10
    with pytest.raises(SemanticRuntimeError) as failure:retrieve_inventory_components(plan(),read,max_rows=8)
    assert failure.value.reason=='result_truncated'


@pytest.mark.parametrize('scope',[{'owner_codes':'A'},{'sku_codes':('',)},{'warehouse_codes':(' W',)}])
def test_invalid_scope_never_becomes_all(scope):
    with pytest.raises(ValueError):plan(**scope)


def test_independent_reference_before_live(facts):
    import runpy
    from pathlib import Path
    reference=runpy.run_path(str(Path(__file__).resolve().parents[2]/'tests/acceptance/check_inventory_components_live.py'))
    stock(facts,quantity=50,lock='5')
    stock(facts,quantity=30,lock='-5',freeze=6)
    stock(facts,owner='B',quantity=-1,freeze=0,changed=None)
    occupy(facts,quantity=5)
    occupy(facts,quantity=10,reference='HJXH-TH001')
    occupy(facts,owner='ORPHAN',reference=None,quantity=-5,created='2026-09-11 00:00:00')
    request=plan()
    expected=reference['expected_groups'](query(facts,reference['reference_sql'](request,'stock')),query(facts,reference['reference_sql'](request,'occupation')))
    actual=result(facts)
    assert all(reference['compare'](actual,expected).values())
    expected[0]['sales_occupation_ea']+=1
    assert not reference['compare'](actual,expected)['all_components_and_diagnostics']


def test_independent_reference_lists_all_source_quality_failures(facts):
    import runpy
    from pathlib import Path
    reference=runpy.run_path(str(Path(__file__).resolve().parents[2]/'tests/acceptance/check_inventory_components_live.py'))
    stock(facts,lock='invalid')
    stock(facts,owner=None)
    occupy(facts,status='bad')
    occupy(facts,created=None)
    request=plan()
    with pytest.raises(reference['InventoryReferenceQualityError']) as failure:
        reference['expected_groups'](query(facts,reference['reference_sql'](request,'stock')),
                                     query(facts,reference['reference_sql'](request,'occupation')))
    assert set(failure.value.quality_failures)=={'invalid_key_count','invalid_stock_lock_count','invalid_occupation_status_count','missing_occupation_date_count'}
    assert 'invalid' not in str(failure.value)


def test_quality_only_query_matches_full_controls_without_quantities(facts):
    from app.agent_core.inventory_components import compile_inventory_quality, QUALITY
    stock(facts,lock='invalid')
    occupy(facts,status='bad',created=None)
    full=query(facts,compile_inventory_components(plan()))[0]
    diagnostic=query(facts,compile_inventory_quality(plan()))
    assert len(diagnostic)==1 and set(diagnostic[0])==set(QUALITY)
    assert diagnostic[0]=={name:full[name] for name in QUALITY}


def test_batches_keep_quality_gates_and_all_keys(facts):
    for i in range(8):stock(facts,sku=str(i))
    waves=[]
    def read(sql,**kwargs):return query(facts,sql)
    def many(statements,**kwargs):
        waves.append(len(statements))
        return [read(sql) for sql in statements]
    actual=retrieve_inventory_components(plan(),read,query_many=many,page_size=1)
    assert actual==result(facts) and waves==[1,4,4]*2
    stock(facts,sku='bad',lock='not numeric')
    waves.clear()
    with pytest.raises(SemanticRuntimeError) as failure:retrieve_inventory_components(plan(),read,query_many=many,page_size=1)
    assert failure.value.reason=='inventory_component_quality_failed' and waves==[1]
    assert failure.value.quality_failures == ('invalid_stock_lock_count',)
    assert 'invalid_stock_lock_count' not in str(failure.value.as_public_dict())


@pytest.mark.parametrize('corruption',['missing','changed','swapped'])
def test_inventory_pages_never_return_partial_or_changed_rows(facts,corruption):
    stock(facts)
    calls=0
    def read(sql,**kwargs):
        nonlocal calls
        calls+=1
        rows=query(facts,sql)
        if calls==2:
            if corruption=='missing':return []
            if corruption=='changed':rows[0]['freeze_ea']+=1
            if corruption=='swapped':rows[0]['result_ordinal']=1
        return rows
    with pytest.raises(SemanticRuntimeError) as failure:retrieve_inventory_components(plan(),read,page_size=1)
    assert failure.value.page_failure == {'missing':'page_length','changed':'content_changed','swapped':'ordinal_changed'}[corruption]


@pytest.mark.parametrize('lock',['1e3','+5','NaN','Infinity','0.00001','999999999999999999999999999999999999'])
def test_unsafe_lock_text_never_silently_casts_to_zero(facts,lock):
    stock(facts,lock=lock)
    with pytest.raises(ValueError,match='quality'):result(facts)


def test_unrelated_source_quality_does_not_pollute_explicit_scope(facts):
    stock(facts,owner='A')
    stock(facts,owner='B',lock='invalid')
    occupy(facts,owner='B',status='invalid')
    assert len(result(facts,plan(owner_codes=('A',))).groups)==1


@pytest.mark.parametrize('instants',[
    {'observed_at':datetime(2026,9,10,12),'occupation_since':datetime(2026,7,12,12)},
    {'observed_at':datetime(2026,9,10,12,tzinfo=ZoneInfo('Asia/Shanghai')),'occupation_since':datetime(2026,9,11,12,tzinfo=ZoneInfo('Asia/Shanghai'))},
])
def test_observation_instant_and_window_are_explicit(instants):
    with pytest.raises(ValueError):InventoryComponentPlan(**instants)
