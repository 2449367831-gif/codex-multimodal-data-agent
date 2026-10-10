import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_order_components import facts as order_facts, header, detail, read
from app.tests.test_return_components import facts as return_facts, header as return_header, detail as return_detail
from app.tests.test_order_return_planning import NOW, selections


class FixtureQuery:
    def __init__(self, orders, returns):
        self.orders = orders.serialize(name='ywzt_ods')
        self.returns = returns.serialize(name='ywzt_ods')
        self.calls = 0

    def query(self, *, sql, purpose):
        self.calls += 1
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
            db.deserialize(self.orders if 'scm_sales_order_' in sql else self.returns, name='ywzt_ods')
            rows = read(db, sql)
        finally:
            db.close()
        return {'status': 'succeeded', 'execution': {'executor_name': 'doris', 'is_mock': False,
                'rows': rows, 'row_count': len(rows), 'truncated': False}}


@pytest.fixture
def chat(tmp_path, order_facts, return_facts, monkeypatch, request):
    header(order_facts)
    detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts)
    if getattr(request, 'param', None) in {'multiowner', 'reference_conflict'}:
        for owner, shipped, returned in [('B', 20, 4), ('C', 40, 5)]:
            header(order_facts, owner=owner)
            detail(order_facts, owner=owner, quantity=shipped)
            return_header(return_facts, owner=owner)
            return_detail(return_facts, owner=owner, quantity=returned,
                          order='OTHER' if request.param == 'reference_conflict' and owner == 'B' else 'S1')
    query = FixtureQuery(order_facts, return_facts)
    runner = FakeChatRunner()
    monkeypatch.setattr('app.agent_core.order_return_planning.return_quantity_contract_active', lambda _: True)
    monkeypatch.setattr('app.agent_core.handlers.order_returns.return_quantity_contract_active', lambda _: True)
    runtime = SemanticQueryRuntime(query_client=query, clock=lambda: NOW)
    store = ChatSessionStore(tmp_path / 'sessions.sqlite3')
    original_reference_time = store.clarification_reference_time
    monkeypatch.setattr(store, 'clarification_reference_time',
                        lambda *args: NOW.timestamp() if original_reference_time(*args) is not None else None)
    app = create_chat_app(runner, session_store=store,
        identity_resolver=IdentityResolver(mode='anonymous'), semantic_runtime=runtime, enforce_loopback=False)
    with TestClient(app) as client:
        token = client.get('/v1/session').json()['csrf_token']
        headers = {'X-CSRF-Token': token}
        conversation = client.post('/v1/conversations', headers=headers).json()['conversation_id']
        yield client, headers, conversation, query, runner


def test_direct_quantity_runs_without_model_and_replay_does_not_query_twice(chat):
    client, headers, conversation, query, runner = chat
    headers = {**headers, 'Idempotency-Key': 'return_quantity_direct_123'}
    body = dict(conversation_id=conversation, question='查询销售订单 S1 的退货数量和净销售数量')
    response = client.post('/v1/chat', headers=headers, json=body)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result['result_state'] == 'live_success', result
    assert len(result['business_tables']) == 2
    assert result['business_tables'][1]['rows'][0]['net_sales_quantity'] == '7'
    calls = query.calls
    assert calls > 0 and not runner.questions
    replay = client.post('/v1/chat', headers=headers, json=body)
    assert replay.json() == result and query.calls == calls


def test_unpublished_question_stops_before_model_and_query(chat, monkeypatch):
    client, headers, conversation, query, runner = chat
    monkeypatch.setattr('app.agent_core.order_return_planning.return_quantity_contract_active', lambda _: False)
    response = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询本月销售订单退货数量和退货金额'))
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result['status'] == 'blocked' and result['result_state'] == 'business_definition_unpublished'
    assert not result['clarification']['required']
    assert '尚未审批发布' in result['message']
    assert not runner.questions and query.calls == 0
    assert 'business_table' not in result


@pytest.mark.parametrize('measure', ['return_quantity_only', 'return_actual_refund'])
def test_all_choices_continue_once_without_reasking_or_model(chat, measure):
    client, headers, conversation, query, runner = chat
    response = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询本月销售订单退货数量和退货金额'))
    assert response.status_code == 200, response.json()
    pending = response.json()
    assert pending['status'] == 'needs_selection', pending
    assert [q['id'] for q in pending['clarification']['questions']] == ['return_measure', 'return_month_window']
    assert not runner.questions and query.calls == 0
    result = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        clarification_token=pending['clarification']['token'],
        clarification_answers=selections(return_measure=measure, return_month_window='return_month_full'))).json()
    assert not runner.questions and not result.get('clarification', {}).get('required'), result
    if measure == 'return_quantity_only':
        assert result['result_state'] == 'live_success', result
        assert len(result['business_tables']) == 2
    else:
        assert result['status'] == 'blocked' and query.calls == 0
        assert '已保留金额需求' in result['message']


def test_unsupported_predicate_does_not_invoke_model_with_reduced_scope(chat):
    client, headers, conversation, query, runner = chat
    result = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询销售订单退货数量，只要成品')).json()
    assert result['status'] == 'blocked'
    assert not runner.questions and not query.calls


def test_background_task_keeps_results_and_is_owner_bound(chat):
    client, headers, conversation, query, runner = chat
    headers = {**headers, 'Idempotency-Key': 'return_quantity_background_123'}
    body = dict(conversation_id=conversation, question='查询销售订单 S1 的退货数量')
    response = client.post('/v1/tasks', headers=headers, json=body)
    assert response.status_code == 202, response.json()
    task_id = response.json()['task_id']
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        terminal = client.get('/v1/tasks/' + task_id).json()
        if terminal['state'] in {'completed', 'failed', 'interrupted'}:
            break
        time.sleep(.02)
    assert terminal['state'] == 'completed', terminal
    assert len(terminal['result']['business_tables']) == 2
    calls = query.calls
    replay = client.post('/v1/tasks', headers=headers, json=body)
    assert replay.json()['task_id'] == task_id
    assert client.get('/v1/tasks/' + task_id).json()['result'] == terminal['result']
    assert query.calls == calls and not runner.questions
    client.cookies.clear()
    client.get('/v1/session')
    assert client.get('/v1/tasks/' + task_id).status_code == 404


def test_other_session_cannot_consume_return_choice(chat):
    client, headers, conversation, query, runner = chat
    pending = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询本月销售订单退货数量')).json()
    token = pending['clarification']['token']
    client.cookies.clear()
    other = client.get('/v1/session').json()
    other_headers = {'X-CSRF-Token': other['csrf_token']}
    other_conversation = client.post('/v1/conversations', headers=other_headers).json()['conversation_id']
    response = client.post('/v1/chat', headers=other_headers, json=dict(conversation_id=other_conversation,
        clarification_token=token, clarification_answers=selections(return_month_window='return_month_full')))
    assert response.status_code in {403, 404, 409, 422}
    assert '查询本月销售订单退货数量' not in response.text
    assert not query.calls and not runner.questions


def test_publication_withdrawn_between_question_and_choice_stops(chat, monkeypatch):
    client, headers, conversation, query, runner = chat
    pending = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询本月销售订单退货数量')).json()
    monkeypatch.setattr('app.agent_core.order_return_planning.return_quantity_contract_active', lambda _: False)
    result = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        clarification_token=pending['clarification']['token'],
        clarification_answers=selections(return_month_window='return_month_full'))).json()
    assert result['status'] == 'blocked' and not result['clarification']['required']
    assert '尚未审批发布' in result['message']
    assert not query.calls and not runner.questions


@pytest.mark.parametrize('chat', ['multiowner'], indirect=True)
def test_multiowner_choice_and_next_request_do_not_inherit_scope(chat):
    client, headers, conversation, query, runner = chat
    pending = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询全部货主、货主 A、B 销售订单 S1 的退货数量')).json()
    assert pending['status'] == 'needs_selection', pending
    assert [q['id'] for q in pending['clarification']['questions']] == ['return_owner_scope']
    assert not query.calls and not runner.questions
    selected = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        clarification_token=pending['clarification']['token'],
        clarification_answers=selections(return_owner_scope='return_owner_list'))).json()
    assert selected['result_state'] == 'live_success', selected
    assert {r['cargo_owner']: r['net_sales_quantity'] for r in selected['business_tables'][1]['rows']} == {'A': '7', 'B': '16'}
    following = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询销售订单 S1 的退货数量')).json()
    assert following['result_state'] == 'live_success', following
    assert {r['cargo_owner'] for r in following['business_tables'][1]['rows']} == {'A', 'B', 'C'}
    assert not runner.questions


@pytest.mark.parametrize('chat', ['reference_conflict'], indirect=True)
def test_multiowner_reference_conflict_never_exposes_partial_tables(chat):
    client, headers, conversation, query, runner = chat
    response = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询货主 A、B 销售订单 S1 的退货数量'))
    result = response.json()
    assert response.status_code == 502, result
    assert result['error']['code'] == 'semantic_query_error'
    assert result['error']['result_state'] == 'failed'
    assert result['error']['details'] == {}
    assert not result.get('business_table') and not result.get('business_tables'), result
    assert query.calls and not runner.questions


@pytest.mark.parametrize('chat', ['multiowner'], indirect=True)
def test_all_five_decisions_survive_public_screening_and_continue(chat):
    client, headers, conversation, query, runner = chat
    pending = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        question='查询所有货主、货主 A、B，全部销售订单、销售订单 S1，全部SKU、SKU 001，全部时间、本月的退货金额')).json()
    assert pending['status'] == 'needs_selection', pending
    assert {q['id'] for q in pending['clarification']['questions']} == {
        'return_measure', 'return_owner_scope', 'return_order_scope', 'return_sku_scope', 'return_date_scope'}
    assert not query.calls and not runner.questions
    result = client.post('/v1/chat', headers=headers, json=dict(conversation_id=conversation,
        clarification_token=pending['clarification']['token'],
        clarification_answers=selections(return_measure='return_quantity_only',
            return_owner_scope='return_owner_list', return_order_scope='return_order_list',
            return_sku_scope='return_sku_list', return_date_scope='return_month_full'))).json()
    assert result['result_state'] == 'live_success', result
    assert {row['cargo_owner'] for row in result['business_tables'][1]['rows']} == {'A', 'B'}
    assert not result['clarification']['required'] and not runner.questions
