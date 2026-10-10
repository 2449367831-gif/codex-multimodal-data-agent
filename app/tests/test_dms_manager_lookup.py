from copy import deepcopy
import re
import sqlite3

import pytest
import sqlglot

from app.agent_core.dms_manager_lookup import canonical_manager_id, compile_manager_lookup, reconcile_manager_lookup, retrieve_manager_lookup
from app.agent_core.semantic_types import SemanticRuntimeError


@pytest.mark.parametrize('raw,expected',[(None,None),('',None),('  ',None),('001','1'),('+001','1'),('-001','-1'),('-000','0'),('9223372036854775807','9223372036854775807'),('-9223372036854775808','-9223372036854775808'),('9223372036854775808',None),('-9223372036854775809',None),('1e2',None),('1.0',None),('12abc',None),('１２',None),('1 2',None),(' +001 ','1')])
def test_integer_identity_without_float_or_coercive_prefix(raw,expected):
    assert canonical_manager_id(raw)==expected


@pytest.fixture
def employees():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.create_function('regexp',2,lambda pattern,value:bool(re.search(pattern,value or '')))
    db.create_function('regexp_like',2,lambda value,pattern:bool(re.search(pattern,value or '')))
    db.create_function('regexp_replace',3,lambda value,pattern,replacement:re.sub(pattern,replacement,value) if value is not None else None)
    db.execute("ATTACH DATABASE ':memory:' AS dms_ods")
    db.execute('CREATE TABLE dms_ods.t_employee(employee_id INTEGER, actual_name TEXT, deleted_flag INTEGER, update_time TEXT)')
    yield db
    db.close()


def add(db,key=1,name='Test Manager',deleted=0,updated='2026-09-10 00:00:00'):
    db.execute('INSERT INTO dms_ods.t_employee VALUES (?,?,?,?)',(key,name,deleted,updated))


def query(db,refs):
    return [dict(row) for row in db.execute(sqlglot.transpile(compile_manager_lookup(refs),read='doris',write='sqlite')[0])]


def test_duplicate_employee_never_multiplies_reference_and_keeps_missing(employees):
    add(employees)
    add(employees)
    refs=('001','+1','2',None,'','not-a-number')
    result=reconcile_manager_lookup(query(employees,refs),refs)
    assert len(result)==len(refs)
    assert [r['match_state'] for r in result]==['matched','matched','not_found','missing_reference','missing_reference','invalid_reference']
    assert result[0]['manager_name']=='Test Manager' and result[0]['source_rows']==2
    assert result[1]['employee_id']=='1'


@pytest.mark.parametrize('change,expected',[({'deleted':1},'deleted_employee'),({'name':None},'missing_name'),({'name':'New Manager'},'matched'),({'deleted':2},'invalid_employee_state')])
def test_latest_employee_before_deletion_or_name_filters(employees,change,expected):
    add(employees,updated='2026-09-09 00:00:00')
    add(employees,**change)
    result=reconcile_manager_lookup(query(employees,('1',)),('1',))
    assert result[0]['match_state']==expected
    if expected!='matched':assert result[0]['manager_name'] is None


@pytest.mark.parametrize('change',[{'name':'Conflict'},{'name':None},{'deleted':1},{'deleted':None}])
def test_equal_timestamp_conflict_is_not_arbitrarily_selected(employees,change):
    add(employees)
    add(employees,**change)
    result=reconcile_manager_lookup(query(employees,('1',)),('1',))
    assert result[0]['match_state']=='conflicting_employee' and result[0]['manager_name'] is None
    assert result[0]['variant_count']==2


def test_bigint_boundaries_and_invalid_values_cannot_match_zero(employees):
    for value in (0,-1,9223372036854775807,-9223372036854775808):add(employees,key=value)
    refs=('abc','1e0','0.0','000','-0','-01','9223372036854775807','9223372036854775808','-9223372036854775808','-9223372036854775809')
    result=reconcile_manager_lookup(query(employees,refs),refs)
    assert [r['match_state'] for r in result]==['invalid_reference']*3+['matched']*4+['invalid_reference','matched','invalid_reference']


@pytest.mark.parametrize('bad',[[],('1','1'),('a'*241,),('line\nbreak',),(1,)])
def test_invalid_reference_batch_rejected(bad):
    with pytest.raises(ValueError):compile_manager_lookup(bad)


def test_empty_batch_never_fetches_all_employees(employees):
    add(employees)
    assert query(employees,())==[]
    assert reconcile_manager_lookup([],())==[]


@pytest.mark.parametrize('corruption',['missing','duplicate','wrong_reference','wrong_id','wrong_count','false_match','leaked_name','fractional_count'])
def test_result_reconciliation_rejects_corruption(employees,corruption):
    add(employees)
    refs=('1','2')
    rows=query(employees,refs)
    if corruption=='missing':rows.pop()
    if corruption=='duplicate':rows[1]=deepcopy(rows[0])
    if corruption=='wrong_reference':rows[0]['manager_reference']='3'
    if corruption=='wrong_id':rows[0]['employee_id']='3'
    if corruption=='wrong_count':rows[0]['group_count']=3
    if corruption=='false_match':rows[1]['match_state']='matched'
    if corruption=='leaked_name':rows[1]['manager_name']='Guessed'
    if corruption=='fractional_count':rows[0]['source_rows']=1.5
    with pytest.raises(ValueError):reconcile_manager_lookup(rows,refs)


def test_missing_timestamp_is_not_a_verified_current_name(employees):
    add(employees,updated=None)
    with pytest.raises(ValueError,match='version timestamp'):
        reconcile_manager_lookup(query(employees,('1',)),('1',))


@pytest.mark.parametrize('raw',['\u00a01\u00a0','\u30001\u3000',"1' OR 1=1 --",r'1\2','+', '-', '0'*5000+'1','9'*5000])
def test_identity_corpus_and_literal_boundaries(employees,raw):
    if len(raw)>240:
        assert canonical_manager_id(raw)==('1' if raw.endswith('1') else None)
        return
    add(employees,key=0)
    add(employees,key=1)
    result=reconcile_manager_lookup(query(employees,(raw,)),(raw,))
    assert result[0]['match_state']=='invalid_reference'
    assert result[0]['source_rows']==0


def test_complete_stable_retrieval_and_empty_no_query(employees):
    add(employees)
    calls=[]
    def execute(sql,*,purpose):
        calls.append(sql)
        return [dict(row) for row in employees.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]
    assert retrieve_manager_lookup((),execute)==[] and calls==[]
    result=retrieve_manager_lookup(('1','2'),execute)
    assert len(calls)==2 and len(result)==2
    assert result[0]['match_state']=='matched' and result[1]['match_state']=='not_found'


def test_changed_source_is_manager_failure_not_sales_failure(employees):
    add(employees)
    calls=[]
    def execute(sql,*,purpose):
        calls.append(sql)
        if len(calls)==2:
            employees.execute("UPDATE dms_ods.t_employee SET actual_name='Changed'")
        return [dict(row) for row in employees.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_manager_lookup(('1',),execute)
    assert failure.value.reason=='dms_manager_reconciliation_failed'


def test_lookup_preserves_query_timeout():
    def execute(sql,*,purpose):
        raise SemanticRuntimeError('timeout',reason='query_timeout')
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_manager_lookup(('1',),execute)
    assert failure.value.reason=='query_timeout'
