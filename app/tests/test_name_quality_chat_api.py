from copy import deepcopy
from datetime import datetime

from fastapi.testclient import TestClient
import pytest
import sqlglot

from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.sku_name_quality import compile_sku_name_quality
from app.query_scope import QueryScope
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_sales_aggregation import facts
from app.tests.test_sku_name_quality import named_facts
from app.tests.test_name_quality_route import QUESTION


@pytest.mark.parametrize('option',['sku_name_actual','sku_name_defer'])
def test_select_once_then_keep_result_without_model_or_cross_user_access(named_facts,tmp_path,monkeypatch,option):
    monkeypatch.setattr('app.agent_core.name_quality_planning.active_name_contract',lambda _:True)
    sql=compile_sku_name_quality(start=None,end=None,scope=QueryScope(sku_codes=('A',)))
    rows=[dict(r) for r in named_facts.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]
    class Query:
        calls=0
        def query(self,**kwargs):
            self.calls+=1
            return {'status':'succeeded','execution':{'executor_name':'doris','is_mock':False,'rows':deepcopy(rows),'row_count':len(rows),'truncated':False}}
    class Runner(FakeChatRunner):
        def run(self,*args,**kwargs):
            raise AssertionError('name selection must not call the model')
    query=Query()
    runtime=SemanticQueryRuntime(query_client=query,clock=lambda:datetime(2026,9,10,12))
    app=create_chat_app(Runner(),semantic_runtime=runtime,session_store=ChatSessionStore(tmp_path/'sessions.sqlite3'),identity_resolver=IdentityResolver(mode='anonymous'),enforce_loopback=False)
    with TestClient(app) as client,TestClient(app) as other:
        session=client.get('/v1/session').json()
        headers={'X-CSRF-Token':session['csrf_token'],'Idempotency-Key':'name-first-123456789'}
        body={'question':QUESTION.replace('`QY-YWKC500G`','`A`'),'conversation_id':session['conversation_id']}
        first=client.post('/v1/chat',headers=headers,json=body)
        assert first.status_code==200 and first.json()['clarification']['required']
        card=first.json()['clarification']
        assert len(card['questions'])==1 and query.calls==0
        choice={'conversation_id':session['conversation_id'],'question':'确认并继续','clarification_token':card['token'],
                'clarification_answers':[{'question_id':'sku_name_outbound_basis','option_ids':[option]}]}
        selected_headers={**headers,'Idempotency-Key':'name-selected-123456789'}
        second=client.post('/v1/chat',headers=selected_headers,json=choice)
        assert second.status_code==200, second.json()
        result=second.json()
        if option=='sku_name_defer':
            assert result['status']=='blocked' and not result['clarification']['required']
            assert 'business_table' not in result and query.calls==0
            replay=client.post('/v1/chat',headers=selected_headers,json=choice)
            assert replay.json()['status']=='blocked' and query.calls==0
            return
        assert result['result_state']=='live_success' and not result['clarification']['required']
        assert len(result['business_table']['columns'])==12 and len(result['business_table']['notes'])==3
        assert query.calls==2
        replay=client.post('/v1/chat',headers=selected_headers,json=choice)
        assert replay.json()['business_table']==result['business_table'] and query.calls==2
        foreign=other.get('/v1/session').json()
        rejected=other.post('/v1/chat',headers={'X-CSRF-Token':foreign['csrf_token']},json=choice)
        assert rejected.status_code>=400 and query.calls==2
