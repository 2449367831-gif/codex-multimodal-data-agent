"""Conflict proposals are choices, not authority to publish a metric."""

from copy import deepcopy
from datetime import datetime
from itertools import product

import pytest
from fastapi.testclient import TestClient

from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore, ClarificationValidationError
from app.codex_agent import interaction_envelope, reviewed_clarification_for_question
from app.tests.test_chat_api import FakeChatRunner


QUESTION = '“库存周转天数”有两个历史说法：A 用当前实时可用库存除以近 7 天平均销售出库；B 用月初库存和本月累计出库计算。现在只说“算本月周转”，请不要直接猜，列出需要确认的选项。'
OPTIONS = {
    'tc_variant': ('tc_a', 'tc_b'),
    'tc_stock': ('tc_realtime', 'tc_month_start'),
    'tc_sales': ('tc_actual', 'tc_allocated'),
    'tc_returns': ('tc_keep_returns', 'tc_deduct_returns'),
    'tc_scope': ('tc_all', 'tc_specified'),
    'tc_window': ('tc_7days', 'tc_mtd', 'tc_custom'),
}


def answers(values=None):
    values = values or ('tc_a', 'tc_realtime', 'tc_actual', 'tc_keep_returns', 'tc_all', 'tc_7days')
    return [{'question_id': key, 'option_ids': [value]} for key, value in zip(OPTIONS, values)]


@pytest.mark.parametrize('question', [QUESTION, '库存周转存在口径冲突：当前实时可用库存和近7天日均出库，或月初库存和本月累计出库，请一次确认。'])
def test_conflict_collects_all_decisions_without_numeric_route(question):
    card = reviewed_clarification_for_question(question)
    assert RequestPlanner().classify(question) is None
    assert card['outcome'] == 'clarification_required'
    assert {q['id']: tuple(o['id'] for o in q['options']) for q in card['questions']} == OPTIONS
    assert card['questions'][-1]['date_range_option'] == 'tc_custom'
    assert all(q['selection'] == 'single' for q in card['questions'])


def test_ordinary_approved_turnover_not_reclassified():
    from app.agent_core.turnover_conflict import turnover_conflict_clarification
    for question in ['算本月库存周转', '上月库存周转', '用可用成品库存加在途除以上月日均实际出库']:
        assert turnover_conflict_clarification(question) is None


def test_no_combination_of_draft_choices_becomes_a_query():
    from app.agent_core.turnover_conflict import selected_turnover_conflict_reason
    for values in product(*OPTIONS.values()):
        selected = answers(values)
        if values[-1] == 'tc_custom':
            selected[-1]['date_range'] = {'start': '2026-08-01', 'end': '2026-09-01'}
        assert RequestPlanner().plan_selected(QUESTION, selected) is None
        reason = selected_turnover_conflict_reason(QUESTION, selected)
        assert '无需重复选择' in reason
        assert len(reason) <= 160


def store_card(tmp_path):
    store = ChatSessionStore(tmp_path / 'chat.sqlite3')
    session = store.ensure_session(None, None)
    conversation = store.create_conversation(session)
    token = store.create_clarification_token(session, conversation, reviewed_clarification_for_question(QUESTION)['questions'], source_question=QUESTION)
    return store, session, conversation, token


@pytest.mark.parametrize('dates', [None, {}, {'start': '2026-02-30', 'end': '2026-03-02'}, {'start': '2026-09-01', 'end': '2026-08-01'}, {'start': '2026-08-01', 'end': '2026-08-01'}, {'start': '20260801', 'end': '2026-09-01'}, {'start': '2026-08-01', 'end': '2026-09-01', 'sql': 'bad'}])
def test_bad_dates_do_not_consume_token(tmp_path, dates):
    store, session, conversation, token = store_card(tmp_path)
    selected = answers()
    selected[-1]['option_ids'] = ['tc_custom']
    if dates is not None:
        selected[-1]['date_range'] = dates
    with pytest.raises(ClarificationValidationError):
        store.validate_clarification_token(session, conversation, token, selected)
    assert store.has_pending_clarification(session, conversation)


def test_dates_survive_rotation_and_only_apply_to_custom_choice(tmp_path):
    store, session, conversation, token = store_card(tmp_path)
    resumed = store.resume_clarification(session, conversation)
    assert resumed['draft_id'] == store.clarification_draft_id(session, conversation, token)
    assert resumed['questions'][-1]['date_range_option'] == 'tc_custom'
    selected = answers()
    selected[-1]['date_range'] = {'start': '2026-08-01', 'end': '2026-09-01'}
    with pytest.raises(ClarificationValidationError):
        store.validate_clarification_token(session, conversation, token, selected)
    selected[-1]['option_ids'] = ['tc_custom']
    actual = store.validate_clarification_token(session, conversation, resumed['token'], selected)
    assert actual == selected
    with pytest.raises(ClarificationValidationError):
        store.validate_clarification_token(session, conversation, token, selected)


def test_draft_identity_never_crosses_owner_or_request(tmp_path):
    store, session, conversation, token = store_card(tmp_path)
    original = store.clarification_draft_id(session, conversation, token)
    other = store.ensure_session(None, None)
    other_conversation = store.create_conversation(other)
    assert store.clarification_draft_id(other, other_conversation, token) is None
    next_token = store.create_clarification_token(session, conversation, reviewed_clarification_for_question(QUESTION)['questions'], source_question=QUESTION)
    assert store.clarification_draft_id(session, conversation, next_token) != original


def test_card_cannot_change_date_contract_or_cross_question_options():
    import json
    card = reviewed_clarification_for_question(QUESTION)
    forged = deepcopy(card)
    forged['questions'][-1]['date_range_option'] = 'tc_mtd'
    assert interaction_envelope(json.dumps(forged))['outcome'] == 'blocked'
    forged = deepcopy(card)
    forged['questions'][0]['options'][0] = {'id': 'tc_actual', 'label': 'actual'}
    assert interaction_envelope(json.dumps(forged))['outcome'] == 'blocked'


@pytest.mark.parametrize('endpoint', ['/v1/chat', '/v1/tasks'])
@pytest.mark.parametrize('custom', [False, True])
def test_chat_closes_once_without_model_or_database(tmp_path, endpoint, custom):
    class NoQuery:
        def query(self, **kwargs):
            raise AssertionError('draft conflict must not access Doris')
    class Runner(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError('reviewed conflict must not call the model')
    app = create_chat_app(Runner(), semantic_runtime=SemanticQueryRuntime(query_client=NoQuery(), clock=lambda: datetime(2026, 9, 11, 12)), session_store=ChatSessionStore(tmp_path / 'chat.sqlite3'), identity_resolver=IdentityResolver(mode='anonymous'), enforce_loopback=False)
    with TestClient(app) as client:
        def result_body(response, expected_task_state='completed'):
            import time
            assert response.status_code in {200, 202}, response.json()
            body = response.json()
            if endpoint == '/v1/tasks':
                for _ in range(200):
                    if body['state'] not in {'queued', 'running'}:
                        break
                    time.sleep(0.01)
                    body = client.get('/v1/tasks/' + body['task_id']).json()
                assert body['state'] == expected_task_state, body
                assert body['http_status'] == 200
                body = body['result']
            return body
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token']}
        first = client.post(endpoint, headers={**headers, 'Idempotency-Key': 'conflict-initial-1234'}, json={'conversation_id': session['conversation_id'], 'question': QUESTION})
        initial = result_body(first)
        session['conversation_id'] = initial['conversation_id']
        card = initial['clarification']
        assert len(card['questions']) == 6
        resumed_response = client.post('/v1/clarification/resume', headers=headers, json={'conversation_id': session['conversation_id']})
        assert resumed_response.status_code == 200, resumed_response.json()
        resumed = resumed_response.json()['clarification']
        assert resumed['draft_id'] == card['draft_id']
        choice = {'conversation_id': session['conversation_id'], 'clarification_token': card['token'], 'clarification_answers': answers()}
        if custom:
            choice['clarification_answers'][-1].update(option_ids=['tc_custom'], date_range={'start':'2026-08-01', 'end':'2026-09-01'})
        result = client.post(endpoint, headers={**headers, 'Idempotency-Key': 'conflict-selection-1234'}, json=choice)
        body = result_body(result, expected_task_state='failed')
        assert body['status'] == 'blocked'
        assert body['result_state'] == 'business_definition_unpublished'
        assert not body['clarification']['required'] and not body.get('business_table')
        assert '无需重复选择' in body['message']
