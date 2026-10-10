from copy import deepcopy

import pytest

from app.agent_core.dms_manager_orders import DmsManagerOrderPlan, compile_manager_groups, reconcile_manager_groups, retrieve_manager_groups
from app.tests.test_dms_manager_orders import facts, add, employee, query, result


def grouped(db,plan=None):
    plan=plan or DmsManagerOrderPlan()
    return reconcile_manager_groups(query(db,compile_manager_groups(plan)),plan)


def test_summary_matches_independent_order_regrouping(facts):
    employee(facts)
    employee(facts)
    for i in range(7):add(facts,code=f'O{i}',row_id=i,manager='+1' if i%2 else '001')
    add(facts,code='missing',manager=None,name=None,province=None)
    add(facts,code='invalid',manager='abc',updated=None)
    summary,details=grouped(facts),result(facts)
    assert summary.groups==details.groups
    assert summary.order_count==9
    assert summary.missing_manager_order_count==2
    assert summary.missing_updated_count==1


def test_budget_counts_groups_not_orders(facts):
    employee(facts)
    for i in range(600):add(facts,code=f'O{i}',row_id=i)
    calls=[]
    def execute(sql,*,purpose):
        calls.append(sql)
        return query(facts,sql)
    summary=retrieve_manager_groups(DmsManagerOrderPlan(),execute,max_rows=2)
    assert summary.order_count==600 and len(summary.groups)==1 and len(calls)==2


def test_unmatched_orders_and_all_reference_groups(facts):
    for i in range(205):add(facts,code=f'O{i}',row_id=i,manager=str(i))
    summary=grouped(facts)
    assert summary.order_count==summary.missing_manager_order_count==205
    assert len(summary.groups)==205


@pytest.mark.parametrize('corrupt',['missing','duplicate','count','total','missing_manager','updated','manager_name','identity','kind'])
def test_summary_controls_reject_corruption(facts,corrupt):
    employee(facts)
    add(facts)
    rows=query(facts,compile_manager_groups(DmsManagerOrderPlan()))
    group=next(row for row in rows if row['row_kind']=='group')
    if corrupt=='missing':rows.remove(group)
    if corrupt=='duplicate':rows.append(deepcopy(group))
    if corrupt=='count':group['order_count']=2
    if corrupt=='total':rows[0]['total_order_count']=2
    if corrupt=='missing_manager':rows[0]['missing_manager_order_count']=1
    if corrupt=='updated':group['group_latest_updated']='2000-01-01'
    if corrupt=='manager_name':group['manager_name']=None
    if corrupt=='identity':group['manager_key']='invalid'
    if corrupt=='kind':group['row_kind']='order'
    with pytest.raises(ValueError):reconcile_manager_groups(rows,DmsManagerOrderPlan())


def test_empty_and_conflicting_source(facts):
    assert grouped(facts).groups==[]
    add(facts)
    add(facts,name='Conflict')
    with pytest.raises(ValueError,match='quality'):grouped(facts)


def test_independent_prejoin_group_reference_before_live(facts):
    from importlib.util import module_from_spec,spec_from_file_location
    from pathlib import Path
    spec=spec_from_file_location('group_reference',Path(__file__).resolve().parents[2]/'tests/acceptance/check_dms_manager_orders_live.py')
    reference=module_from_spec(spec)
    spec.loader.exec_module(reference)
    employee(facts)
    employee(facts)
    for i in range(4):add(facts,code=f'O{i}',row_id=i,manager='+1' if i%2 else '001')
    add(facts,code='missing',manager='99',province=None,name=None,updated=None)
    plan=DmsManagerOrderPlan()
    header_groups=reference.check_reference_groups(query(facts,reference.reference_group_sql(plan)))
    refs=tuple(dict.fromkeys(row['manager_reference'] for row in header_groups))
    employees=query(facts,reference.manager_reference.reference_sql(refs))
    expected=reference.expected_group_report(header_groups,employees)
    assert all(reference.compare_groups(grouped(facts),expected).values())
    expected['order_count']+=1
    assert not reference.compare_groups(grouped(facts),expected)['order_and_missing_attribute_controls']


def test_grouped_batch_path_preserves_page_quality_checks(facts):
    from app.agent_core.semantic_types import SemanticRuntimeError
    for i in range(8):add(facts,code=f'O{i}',row_id=i,manager=str(i))
    waves=[]
    def execute(sql,*,purpose):return query(facts,sql)
    def many(statements,*,purpose,timeout_seconds):
        waves.append(len(statements))
        return [execute(sql,purpose=purpose) for sql in statements]
    plan=DmsManagerOrderPlan()
    actual=retrieve_manager_groups(plan,execute,query_many=many,page_size=1)
    assert actual==grouped(facts) and waves==[1,4,4]*2
    def corrupt(*args,**kwargs):
        batches=many(*args,**kwargs)
        if len(batches)>1:batches[-1][0]['version_conflict_count']=1
        return batches
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_manager_groups(plan,execute,query_many=corrupt,page_size=1)
    assert failure.value.reason=='dms_manager_order_quality_failed'


@pytest.mark.parametrize('detail',[False,True])
def test_explicit_large_pages_preserve_every_group_and_order(facts,detail):
    from app.agent_core.dms_manager_orders import retrieve_manager_orders
    import sqlglot

    employee(facts)
    for i in range(2001):
        add(facts,code=f'O{i:05}',customer=f'C{i:05}',row_id=i)
    widths=[]
    limits=[]
    def execute(sql,*,purpose):
        limits.append(int(sqlglot.parse_one(sql,read='doris').args['limit'].expression.this))
        return query(facts,sql)
    def many(statements,*,purpose,timeout_seconds):
        widths.append(len(statements))
        return [execute(sql,purpose=purpose) for sql in statements]
    retrieve=retrieve_manager_orders if detail else retrieve_manager_groups
    actual=retrieve(DmsManagerOrderPlan(),execute,query_many=many,page_size=2000,page_cap=2000)
    assert actual.order_count==2001 and len(actual.groups)==2001
    assert {row['customer_code'] for row in actual.groups}=={f'C{i:05}' for i in range(2001)}
    assert all(row['order_count']==1 and row['manager_name']=='Manager' for row in actual.groups)
    if detail:
        assert {row['sales_order_code'] for row in actual.orders}=={f'O{i:05}' for i in range(2001)}
    assert widths==[1,1]*2 and limits==[2000]*4


@pytest.mark.parametrize('budgets',[
    {'page_size':2000}, {'page_size':2000,'page_cap':500},
    {'page_size':2000,'page_cap':10001}, {'page_size':2000,'page_cap':True},
])
def test_larger_dms_pages_require_valid_explicit_cap_before_query(budgets):
    from app.agent_core.semantic_types import SemanticRuntimeError
    def forbidden(*args,**kwargs):
        pytest.fail('invalid page budget reached database')
    with pytest.raises(SemanticRuntimeError):
        retrieve_manager_groups(DmsManagerOrderPlan(),forbidden,**budgets)
