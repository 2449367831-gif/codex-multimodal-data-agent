"""Public chat lifecycle for SO07 using synthetic order facts only."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_sales_order_details import orders
from app.tests.test_sales_order_pages import Query
from app.tests.test_so07_route import NOW


class FixtureQuery:
    def __init__(self, orders):
        self.source = orders.serialize(name='ywzt_ods')
        self.calls = 0

    def query(self, *, sql, purpose):
        self.calls += 1
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
            db.deserialize(self.source, name='ywzt_ods')
            rows = Query(db)(sql, purpose=purpose)
        finally:
            db.close()
        return {'status':'succeeded', 'execution':{'executor_name':'doris', 'is_mock':False,
                'rows':rows, 'row_count':len(rows), 'truncated':False}}


@pytest.fixture
def chat(tmp_path, orders, monkeypatch):
    query = FixtureQuery(orders)
    runner = FakeChatRunner()
    monkeypatch.setattr('app.agent_core.so07_planning.so07_contract_active', lambda _: True)
    monkeypatch.setattr('app.agent_core.handlers.so07.so07_contract_active', lambda _: True)
    runtime = SemanticQueryRuntime(query_client=query, clock=lambda: NOW)
    store = ChatSessionStore(tmp_path / 'sessions.sqlite3')
    reference = store.clarification_reference_time
    monkeypatch.setattr(store, 'clarification_reference_time',
        lambda *args: NOW.timestamp() if reference(*args) is not None else None)
    app = create_chat_app(runner, session_store=store, semantic_runtime=runtime,
                         identity_resolver=IdentityResolver(mode='anonymous'), enforce_loopback=False)
    with TestClient(app) as client:
        token = client.get('/v1/session').json()['csrf_token']
        headers = {'X-CSRF-Token':token}
        conversation = client.post('/v1/conversations',headers=headers).json()['conversation_id']
        yield client, headers, conversation, query, runner


def ask(chat, question, **kwargs):
    client, headers, conversation, _, _ = chat
    return client.post('/v1/chat',headers=headers,json=dict(conversation_id=conversation,question=question,**kwargs))


def test_direct_chat_returns_raw_table_and_never_uses_model(chat):
    result = ask(chat,'查询昨天SO07订单请求数量').json()
    assert result['result_state'] == 'live_success', result
    assert len(result['business_table']['rows']) == 4
    assert result['business_table']['summary'] == [{'label':'订单数','value':'3','unit':'单'}]
    assert {r['request_quantity'] for r in result['business_table']['rows']} == {'2.5','3','0','4'}
    assert chat[3].calls > 0 and not chat[4].questions


def test_scope_resets_even_with_same_backend_context(chat):
    first = ask(chat,'查询昨天货主 A 仓库编码 W1 SKU S1 的SO07订单请求数量').json()
    assert first['result_state'] == 'live_success', first
    assert len(first['business_table']['rows']) == 1
    result = ask(chat,'查询SO07订单请求数量').json()
    assert result['result_state'] == 'live_success', result
    assert {r['sales_order_code'] for r in result['business_table']['rows']} == {'SO1','SO2','SO3','OLD'}
    assert {r['cargo_owner'] for r in result['business_table']['rows']} == {'A','B'}
    assert not chat[4].questions


def test_empty_is_success_not_failure(chat):
    result = ask(chat,'查询今天SO07订单请求数量').json()
    assert result['result_state'] == 'no_data', result
    assert result['business_table']['rows'] == []
    assert not result['clarification']['required'] and not chat[4].questions


@pytest.mark.parametrize('reason,state', [('backend_rejected','rejected'), ('query_failed','failed'),
    ('backend_unavailable','failed'), ('query_timeout','failed'), ('result_truncated','truncated')])
def test_execution_failures_are_distinct_and_never_expose_tables(chat, monkeypatch, reason, state):
    from app.query_outcomes import DorisResultError
    failure = DorisResultError(reason)
    def reject(**kwargs):
        raise failure
    monkeypatch.setattr(chat[3], 'query', reject)
    response = ask(chat,'查询昨天SO07订单请求数量')
    result = response.json()
    assert response.status_code == failure.http_status, result
    assert result['error']['code'] == failure.code and result['error']['result_state'] == state
    assert result['error']['details'] == {} and not result.get('business_table')
    assert not chat[4].questions


def test_another_owner_cannot_submit_original_choices(chat):
    client, headers, conversation, query, runner = chat
    pending = ask(chat,'查询本月SO07订单请求数量').json()
    client.cookies.clear()
    other = client.get('/v1/session').json()
    other_headers = {'X-CSRF-Token':other['csrf_token']}
    other_conversation = client.post('/v1/conversations',headers=other_headers).json()['conversation_id']
    result = client.post('/v1/chat',headers=other_headers,json=dict(conversation_id=other_conversation,
        clarification_token=pending['clarification']['token'],clarification_answers=[
            {'question_id':'so07_month_window','option_ids':['so07_month_full']}]))
    assert result.status_code in {403,404,409,422}
    assert not query.calls and not runner.questions


@pytest.mark.parametrize('question', ['查询SO07订单请求数量，只要成品',
    '查询SO07订单请求数量，按出库时间', '查询2026-02-30至2026-09-10的SO07订单请求数量'])
def test_unsupported_scope_never_falls_through_to_model(chat, question):
    result = ask(chat,question).json()
    assert result['status'] == 'blocked', result
    assert not result.get('business_table') and not result['clarification']['required']
    assert not chat[3].calls and not chat[4].questions


def test_unpublished_blocks_before_query_or_model(chat, monkeypatch):
    monkeypatch.setattr('app.agent_core.so07_planning.so07_contract_active', lambda _: False)
    result = ask(chat,'查询昨天SO07订单请求数量').json()
    assert result['status'] == 'blocked' and '尚未审批发布' in result['message'], result
    assert not chat[3].calls and not chat[4].questions


def test_publication_rechecked_by_executor_after_planning(chat, monkeypatch):
    monkeypatch.setattr('app.agent_core.handlers.so07.so07_contract_active', lambda _: False)
    response = ask(chat,'查询昨天SO07订单请求数量')
    assert response.status_code >= 400
    assert response.json()['error']['code'] == 'semantic_query_error'
    assert not response.json().get('business_table') and not chat[3].calls and not chat[4].questions


def test_all_choices_round_trip_once_and_are_bound_to_question_time(chat):
    client, headers, conversation, query, runner = chat
    pending = ask(chat,'查询本月全部时间全部货主货主 A 全部仓库仓库编码 W1 全部SKU SKU S1 的SO07订单请求数量').json()
    assert pending['status'] == 'needs_selection', pending
    questions = pending['clarification']['questions']
    assert {q['id'] for q in questions} == {'so07_date_scope','so07_owner_scope','so07_warehouse_scope','so07_sku_scope'}
    assert not query.calls and not runner.questions
    selected = [{'question_id':q['id'],'option_ids':[q['options'][0]['id']]} for q in questions]
    result = client.post('/v1/chat',headers=headers,json=dict(conversation_id=conversation,
        clarification_token=pending['clarification']['token'],clarification_answers=selected)).json()
    assert result['result_state'] == 'live_success', result
    assert len(result['business_table']['rows']) == 1
    assert not result['clarification']['required'] and not runner.questions


def test_withdrawn_publication_does_not_reask_or_query(chat, monkeypatch):
    client, headers, conversation, query, runner = chat
    pending = ask(chat,'查询本月SO07订单请求数量').json()
    assert pending['status'] == 'needs_selection', pending
    monkeypatch.setattr('app.agent_core.so07_planning.so07_contract_active', lambda _: False)
    result = client.post('/v1/chat',headers=headers,json=dict(conversation_id=conversation,
        clarification_token=pending['clarification']['token'],clarification_answers=[
            {'question_id':'so07_month_window','option_ids':['so07_month_full']}])).json()
    assert result['status'] == 'blocked' and not result['clarification']['required']
    assert '尚未审批发布' in result['message'] and not query.calls and not runner.questions


def test_background_result_replay_is_owned_and_does_not_query_twice(chat):
    client, headers, conversation, query, runner = chat
    body = dict(conversation_id=conversation,question='查询昨天SO07订单请求数量')
    headers = {**headers,'Idempotency-Key':'so07_background_test_123'}
    response = client.post('/v1/tasks',headers=headers,json=body)
    assert response.status_code == 202, response.json()
    task = response.json()['task_id']
    terminal = None
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        terminal = client.get('/v1/tasks/'+task).json()
        if terminal['state'] in {'completed','failed','interrupted'}: break
        time.sleep(.02)
    assert terminal['state'] == 'completed', terminal
    assert terminal['result']['result_state'] == 'live_success'
    calls = query.calls
    assert client.post('/v1/tasks',headers=headers,json=body).json()['task_id'] == task
    assert client.get('/v1/tasks/'+task).json()['result'] == terminal['result']
    assert query.calls == calls and not runner.questions
    client.cookies.clear()
    client.get('/v1/session')
    assert client.get('/v1/tasks/'+task).status_code == 404
