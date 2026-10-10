from copy import deepcopy
from datetime import datetime, date

from fastapi.testclient import TestClient
import pytest

from app.agent_core.dms_order_planning import points_order_request
from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import create_chat_app, IdentityResolver, _public_dms_order_tables, ChatExecutionError
from app.chat_sessions import ChatSessionStore
from app.result_pages import ResultPageStore, ResultPageError
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_dms_points_orders import orders, add, query


QUESTION = '查询 DMS 2026 年 1 至 8 月积分兑换订单，按月份统计订单数，并返回客户名称、客户编码和订单号明细。要求排除逻辑删除数据，不要擅自增加 WMS 出库完成条件。'


@pytest.fixture
def released(tmp_path, monkeypatch):
    (tmp_path/'metrics').mkdir()
    (tmp_path/'metrics/dms.points_orders.yaml').write_text('status: active\napproval: {state: approved}\n', encoding='utf-8')
    monkeypatch.setattr('app.agent_core.planning.active_snapshot', lambda _: (tmp_path, 'test'))
    return tmp_path


@pytest.mark.parametrize('text,start,end', [
    (QUESTION, date(2026,1,1),date(2026,9,1)),
    ('查看DMS上月积分订单数和客户名称明细', date(2026,8,1),date(2026,9,1)),
    ('DMS全部积分订单明细，客户编码、订单号', None,None),
    ('查询DMS2026-08-01至2026-08-31积分订单数，按创建时间统计',date(2026,8,1),date(2026,9,1)),
])
def test_explicit_creation_windows_and_all_reset(released,text,start,end):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    request=planner.classify(text)
    assert request and request.route=='dms_points_orders'
    assert (request.start,request.end)==(start,end)
    assert request.scope.owner=='ALL' and request.scope.channel=='ALL'
    assert not request.dms_order_plan.customer_codes


@pytest.mark.parametrize('clause', ['仅已付款','客户编码001','客户名称包含小虎','只要长沙黄兴','已完成出库','按付款时间','订单金额超过100','排除客户编码为空','仅线上','SKU为8030101'])
def test_unknown_predicates_are_never_erased(released,clause):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    assert points_order_request(QUESTION+clause,interval_parser=planner._source_interval,contract_root=released) is None


def test_unpublished_route_and_older_product_variant(released):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    assert points_order_request('查看DMS1-8月积分兑换空气炸锅数量',interval_parser=planner._source_interval,contract_root=released) is None
    (released/'metrics/dms.points_orders.yaml').write_text('status: candidate\napproval: {state: approved}\n',encoding='utf-8')
    assert points_order_request(QUESTION,interval_parser=planner._source_interval,contract_root=released) is None


class NoModel(FakeChatRunner):
    def run(self,*args,**kwargs):
        raise AssertionError('reviewed points orders must not call model')


def test_runtime_uses_bounded_batches_with_complete_execution_evidence(orders,released,monkeypatch):
    from app.agent_core.dms_points_orders import retrieve_dms_points_orders
    from app.tests.test_query_outcomes import payload
    for i in range(8):add(orders,code=f'O{i}',row_id=i+1)
    def retrieve(plan,read,**kwargs):
        return retrieve_dms_points_orders(plan,read,page_size=2,**kwargs)
    monkeypatch.setattr('app.agent_core.handlers.dms_orders.retrieve_dms_points_orders',retrieve)
    class Reader:
        waves=[]
        def query(self,**kwargs):raise AssertionError('batch-capable client must use batch path')
        def query_many(self,*,statements,purpose,timeout_seconds):
            self.waves.append(len(statements))
            return [payload(query(orders,sql)) for sql in statements]
    reader=Reader()
    result=SemanticQueryRuntime(query_client=reader,clock=lambda:datetime(2026,9,10,12)).run_if_supported(QUESTION)
    assert result['result_state']=='live_success' and reader.waves==[1,4,1,4]
    assert result['query_execution']['successful_query_call_count']==10
    assert result['business_tables'][0]['rows']==[{'month':'2026-01','order_count':'8'}]
    assert len(result['business_tables'][1]['rows'])==8


def test_chat_dual_tables_pages_replay_and_owner_isolation(orders,released,tmp_path):
    for i in range(105):
        add(orders, code=f'O{i:03}', customer='000012345678901234567890', name=None if i==0 else 'Test customer',row_id=i+1)
    class Query:
        calls=0
        def query(self,**kwargs):
            self.calls+=1
            rows=query(orders,kwargs['sql'])
            return {'status':'succeeded','execution':{'executor_name':'doris','is_mock':False,'rows':rows,'row_count':len(rows),'truncated':False}}
    reader=Query()
    runtime=SemanticQueryRuntime(query_client=reader,clock=lambda:datetime(2026,9,10,12))
    app=create_chat_app(NoModel(),semantic_runtime=runtime,session_store=ChatSessionStore(tmp_path/'sessions.sqlite3'),identity_resolver=IdentityResolver(mode='anonymous'),enforce_loopback=False)
    with TestClient(app) as client,TestClient(app) as other:
        session=client.get('/v1/session').json()
        headers={'X-CSRF-Token':session['csrf_token'],'Idempotency-Key':'dms-orders-123456789'}
        body={'question':QUESTION,'conversation_id':session['conversation_id']}
        response=client.post('/v1/chat',headers=headers,json=body)
        assert response.status_code==200,response.json()
        result=response.json()
        assert result['result_state']=='live_success'
        monthly,details=result['business_tables']
        assert result['business_table']==monthly
        assert monthly['rows']==[{'month':'2026-01','order_count':'105'}]
        assert len(details['rows'])==100 and details['pagination']['total_rows']==105
        assert details['rows'][0]['customer_name'] is None
        assert details['rows'][0]['customer_code']=='000012345678901234567890'
        rid=details['pagination']['result_id']
        last=client.get(f'/v1/results/{rid}/pages/2')
        assert last.status_code==200 and len(last.json()['business_table']['rows'])==5
        other.get('/v1/session')
        assert other.get(f'/v1/results/{rid}/pages/2').status_code==404
        assert reader.calls==2
        replay=client.post('/v1/chat',headers=headers,json=body)
        assert replay.json()['business_tables']==result['business_tables'] and reader.calls==2
        assert not any(key in result for key in ('query_plan','mcp_calls','sql','rows'))
        assert not any(key in details for key in ('complete_reconciled','table_id','group_count'))


@pytest.mark.parametrize('failure',['missing_table','swapped','unreviewed','not_live','unsafe_second_page'])
def test_no_partial_or_unreviewed_multi_table_result(failure):
    monthly={'format':'table','table_id':'dms_points_orders_monthly','complete_reconciled':True,'rows':[{'month':'2026-01','order_count':'101'}]}
    detail={'format':'table','table_id':'dms_points_orders_detail','complete_reconciled':True,'rows':[{'sales_order_code':f'O{i}','customer_code':None,'customer_name':'Test customer','created_time':'2026-01-01 00:00:00'} for i in range(101)]}
    tables=[monthly,detail]
    if failure=='missing_table':tables.pop()
    if failure=='swapped':tables.reverse()
    if failure=='unsafe_second_page':detail['rows'][100]['customer_name']='SELECT password FROM secrets'
    store=ResultPageStore()
    with pytest.raises(ChatExecutionError):
        _public_dms_order_tables(tables,reviewed=failure!='unreviewed',live=failure!='not_live',pages_store=store,owner='owner')
    assert not store._results


def test_empty_live_report_keeps_both_table_schemas(orders,released):
    class Query:
        def query(self,**kwargs):
            rows=query(orders,kwargs['sql'])
            return {'status':'succeeded','execution':{'executor_name':'doris','is_mock':False,'rows':rows,'row_count':len(rows),'truncated':False}}
    runtime=SemanticQueryRuntime(query_client=Query(),clock=lambda:datetime(2026,9,10,12))
    result=runtime.run_if_supported(QUESTION)
    assert result['result_state']=='no_data'
    tables=_public_dms_order_tables(result['business_tables'],reviewed=True,live=True,pages_store=ResultPageStore(),owner='owner')
    assert len(tables)==2 and all(table['rows']==[] for table in tables)
    assert tables[0]['summary'][0]['value']=='0'
