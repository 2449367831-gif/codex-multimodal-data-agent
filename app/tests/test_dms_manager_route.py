from datetime import date, datetime

from fastapi.testclient import TestClient
import pytest

from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_dms_manager_orders import facts, add, employee, query


QUESTION = '查询 DMS 销售订单的日期、省区、城市经理、客户编码、客户名称和订单数。城市经理通过 `t_sales_order.owner_manager = t_employee.employee_id` 关联，展示 `t_employee.actual_name`；客户名称直接取 `t_sales_order.customer_name`。'


@pytest.fixture
def approved(tmp_path,monkeypatch):
    (tmp_path/'metrics').mkdir()
    for metric in ('dms.manager_orders','dms.city_manager'):
        (tmp_path/f'metrics/{metric}.yaml').write_text('status: active\napproval: {state: approved}\n',encoding='utf-8')
    monkeypatch.setattr('app.agent_core.planning.active_snapshot',lambda _: (tmp_path,'synthetic'))
    return tmp_path


@pytest.mark.parametrize('text,start,end',[
    (QUESTION,None,None),
    (QUESTION.replace('`',''),None,None),
    ('查询DMS昨天销售订单，按日期、省区、城市经理、客户编码、客户名称统计订单数',date(2026,9,9),date(2026,9,10)),
    ('查询DMS2026年1至8月销售订单，返回日期、省区、城市经理、客户编码、客户名称和订单数',date(2026,1,1),date(2026,9,1)),
])
def test_manager_orders_route_dates_and_all_scope(approved,text,start,end):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    plan=planner.classify(text)
    assert plan and plan.route=='dms_manager_orders'
    assert (plan.start,plan.end)==(start,end)
    assert (plan.dms_manager_plan.start,plan.dms_manager_plan.end)==(start,end)
    assert plan.scope.owner==plan.scope.channel=='ALL'
    assert not plan.scope.warehouse_codes and not plan.scope.sku_codes
    assert planner.classify(QUESTION).start is None


@pytest.mark.parametrize('clause',[
    '仅已付款','只要积分订单','只要长沙黄兴','仅线上','客户编码001',
    '省匹配不到时按城市匹配','排除经理为空','按付款时间','昨天及上月',
    '订单数大于10','只查询HJXH货主',
])
def test_unreviewed_manager_predicates_never_disappear(approved,clause):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    assert planner.classify(QUESTION+clause) is None


def test_t27_province_city_fallback_never_uses_t26_native_province_route(approved):
    """T27 needs a reviewed organization source; native order province is not a fallback."""
    question = '查询 DMS 订单省区优先按省匹配组织架构，省匹配不到时按城市匹配，返回匹配来源和未匹配记录'
    planner = RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    assert planner.classify(question) is None


def test_invalid_explicit_date_is_not_all_dates(approved):
    from app.agent_core.semantic_types import SemanticRuntimeError
    with pytest.raises(SemanticRuntimeError) as failure:
        RequestPlanner(clock=lambda:datetime(2026,9,10,12)).classify(QUESTION+'2026-02-30')
    assert failure.value.reason=='date_invalid'


@pytest.mark.parametrize('metric',['dms.manager_orders','dms.city_manager'])
def test_candidate_dependency_cannot_enable_route(approved,metric):
    (approved/f'metrics/{metric}.yaml').write_text('status: candidate\napproval: {state: approved}\n',encoding='utf-8')
    assert RequestPlanner(clock=lambda:datetime(2026,9,10,12)).classify(QUESTION) is None


class NoModel(FakeChatRunner):
    def run(self,*args,**kwargs):
        raise AssertionError('reviewed manager orders must not call model')


def test_chat_manager_table_paging_missing_fields_and_isolation(facts,approved,tmp_path):
    employee(facts)
    for i in range(105):
        add(facts,code=f'O{i:03}',customer=f'{i:022}',name=None if i==0 else 'Test customer',
            row_id=i,manager=None if i==0 else '001',province=None if i==0 else 'Test Province')
    class Reader:
        calls=0
        def query(self,**kwargs):
            self.calls+=1
            rows=query(facts,kwargs['sql'])
            return {'status':'succeeded','execution':{'executor_name':'doris','is_mock':False,'rows':rows,'row_count':len(rows),'truncated':False}}
    reader=Reader()
    runtime=SemanticQueryRuntime(query_client=reader,clock=lambda:datetime(2026,9,10,12))
    app=create_chat_app(NoModel(),semantic_runtime=runtime,session_store=ChatSessionStore(tmp_path/'sessions.sqlite3'),identity_resolver=IdentityResolver(mode='anonymous'),enforce_loopback=False)
    with TestClient(app) as client,TestClient(app) as other:
        session=client.get('/v1/session').json()
        headers={'X-CSRF-Token':session['csrf_token'],'Idempotency-Key':'manager-orders-123456789'}
        body={'question':QUESTION,'conversation_id':session['conversation_id']}
        response=client.post('/v1/chat',headers=headers,json=body)
        assert response.status_code==200,response.json()
        result=response.json()
        assert result['result_state']=='live_success'
        table=result['business_table']
        assert len(table['rows'])==100 and table['pagination']['total_rows']==105
        assert len(table['columns'])==9
        rid=table['pagination']['result_id']
        last=client.get(f'/v1/results/{rid}/pages/2').json()['business_table']
        assert len(last['rows'])==5 and last['notes']==table['notes']
        rows=table['rows']+last['rows']
        assert sum(int(row['order_count']) for row in rows)==105
        assert sum(int(row['missing_manager_order_count']) for row in rows)==1
        missing=next(row for row in rows if row['customer_code']=='0'*22)
        assert missing['manager_name'] is None and missing['customer_name'] is None and missing['province'] is None
        assert missing['manager_state']=='未配置经理'
        other.get('/v1/session')
        assert other.get(f'/v1/results/{rid}/pages/2').status_code==404
        calls=reader.calls
        assert client.post('/v1/chat',headers=headers,json=body).json()['business_table']==table
        assert reader.calls==calls
        assert not any(key in result for key in ('query_plan','mcp_calls','sql','rows'))
        assert not any(key in table for key in ('complete_reconciled','table_id','group_count'))


def test_empty_manager_result_retains_schema(facts,approved):
    class Reader:
        def query(self,**kwargs):
            rows=query(facts,kwargs['sql'])
            return {'status':'succeeded','execution':{'executor_name':'doris','is_mock':False,'rows':rows,'row_count':len(rows),'truncated':False}}
    result=SemanticQueryRuntime(query_client=Reader(),clock=lambda:datetime(2026,9,10,12)).run_if_supported(QUESTION)
    assert result['result_state']=='no_data'
    assert result['business_table']['rows']==[] and len(result['business_table']['columns'])==9


@pytest.mark.parametrize('unsafe',['SELECT password FROM secrets','<script>alert(1)</script>'])
def test_unsafe_later_page_never_publishes_partial_manager_report(facts,approved,tmp_path,unsafe):
    employee(facts)
    for i in range(105):
        add(facts,code=f'O{i:03}',customer=f'C{i:03}',name=unsafe if i==104 else 'Test customer',row_id=i)
    class Reader:
        def query(self,**kwargs):
            rows=query(facts,kwargs['sql'])
            return {'status':'succeeded','execution':{'executor_name':'doris','is_mock':False,'rows':rows,'row_count':len(rows),'truncated':False}}
    runtime=SemanticQueryRuntime(query_client=Reader(),clock=lambda:datetime(2026,9,10,12))
    app=create_chat_app(NoModel(),semantic_runtime=runtime,session_store=ChatSessionStore(tmp_path/'sessions.sqlite3'),identity_resolver=IdentityResolver(mode='anonymous'),enforce_loopback=False)
    with TestClient(app) as client:
        session=client.get('/v1/session').json()
        response=client.post('/v1/chat',headers={'X-CSRF-Token':session['csrf_token']},
            json={'question':QUESTION,'conversation_id':session['conversation_id']})
        assert response.status_code==502
        assert 'business_table' not in response.json() and unsafe not in response.text
        assert not app.state.result_pages._results


def test_original_acceptance_question_is_not_simplified():
    import json
    from pathlib import Path
    cases=json.loads((Path(__file__).resolve().parents[2]/'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))
    assert QUESTION==next(case['question'] for case in cases['cases'] if case['id']=='T26')
