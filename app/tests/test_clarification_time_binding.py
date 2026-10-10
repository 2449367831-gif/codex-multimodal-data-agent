from datetime import date, datetime
from copy import deepcopy
import time
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest
import sqlglot

from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.orchestrator import AgentOrchestrator
from app.agent_core.outbound_clarification import outbound_clarification
from app.agent_core.sales_aggregation import SalesAggregation, compile_sales_aggregation
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore, ClarificationValidationError, ConversationNotFoundError
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_sales_aggregation import facts
from app.tests.test_outbound_clarification import answers


CHOICES = answers(('outbound_sales', 'outbound_actual_qty', 'outbound_shipped_at'))
ZONE = ZoneInfo('Asia/Shanghai')


def test_bound_planner_does_not_mutate_clock_for_later_questions():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 0, 1, tzinfo=ZONE))
    request = planner.plan_selected('昨天出库量', CHOICES, reference_time=datetime(2026, 9, 10, 23, 59, tzinfo=ZONE))
    assert (request.start, request.end) == (date(2026, 9, 9), date(2026, 9, 10))
    later = planner.plan_selected('昨天出库量', CHOICES)
    assert (later.start, later.end) == (date(2026, 9, 10), date(2026, 9, 11))


@pytest.mark.parametrize('invalid', ['2026-09-10', 0, datetime(2026, 9, 10)])
def test_bound_planner_requires_an_aware_internal_timestamp(invalid):
    with pytest.raises(ValueError):
        RequestPlanner().plan_selected('昨天出库量', CHOICES, reference_time=invalid)


def test_legacy_executor_cannot_drop_the_trusted_date_binding():
    class Legacy:
        def run_selected_if_supported(self, source, answers, *, context_reused=False):
            raise AssertionError('an unbound executor must not run')
    assert AgentOrchestrator(Legacy()).run_selected_if_supported(
        '昨天出库量', CHOICES, reference_time=datetime(2026, 9, 10, tzinfo=ZONE)
    ) is None


@pytest.mark.parametrize('refresh', [False, True])
@pytest.mark.parametrize('queued', [False, True])
def test_midnight_selection_and_refreshed_token_keep_original_date(facts, tmp_path, monkeypatch, refresh, queued):
    current = [datetime(2026, 9, 10, 23, 59, tzinfo=ZONE)]
    monkeypatch.setattr('app.chat_sessions.time.time', lambda: current[0].timestamp())
    plans = []
    sql = compile_sales_aggregation(SalesAggregation(), start=date(2026, 9, 9), end=date(2026, 9, 10))
    fixture_rows = [dict(row) for row in facts.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
    class Query:
        def query(self, *, sql, purpose):
            rows = deepcopy(fixture_rows)
            return {'status': 'succeeded', 'execution': {'executor_name': 'doris', 'is_mock': False, 'rows': rows, 'row_count': len(rows), 'truncated': False}}
    class Runtime(SemanticQueryRuntime):
        def execute_plan(self, request, **kwargs):
            plans.append(request)
            return super().execute_plan(request, **kwargs)
    class Runner(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError('selection must not call the model')
    runtime = Runtime(query_client=Query(), clock=lambda: current[0])
    store = ChatSessionStore(tmp_path / 'chat.sqlite3')
    app = create_chat_app(Runner(), semantic_runtime=runtime, session_store=store,
                          identity_resolver=IdentityResolver(mode='anonymous'), enforce_loopback=False)
    if queued:
        claim = app.state.task_store.claim
        def delayed_claim(*args, **kwargs):
            result = claim(*args, **kwargs)
            current[0] = datetime(2026, 9, 11, 0, 1, tzinfo=ZONE)
            return result
        monkeypatch.setattr(app.state.task_store, 'claim', delayed_claim)
    with TestClient(app) as client:
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token']}
        body = {'conversation_id': session['conversation_id']}
        first = client.post('/v1/tasks' if queued else '/v1/chat', headers={**headers, 'Idempotency-Key': 'time-original-1234'}, json={**body, 'question': '昨天出库量'})
        assert first.status_code == (202 if queued else 200)
        result = first.json()
        if queued:
            for _ in range(200):
                result = client.get('/v1/tasks/' + result['task_id']).json()
                if result['state'] not in {'queued', 'running'}:
                    break
                time.sleep(0.01)
            assert result['state'] == 'completed', result
            result = result['result']
        body['conversation_id'] = result['conversation_id']
        card = result['clarification']
        current[0] = datetime(2026, 9, 11, 0, 1, tzinfo=ZONE)
        if refresh:
            resumed = client.post('/v1/clarification/resume', headers=headers, json=body)
            assert resumed.status_code == 200, resumed.json()
            card = resumed.json()['clarification']
        choice = {
            **body, 'clarification_token': card['token'], 'clarification_answers': CHOICES,
        }
        forged = client.post('/v1/chat', headers=headers, json={**choice, 'reference_time': '2099-01-01T00:00:00+08:00'})
        assert forged.status_code == 422 and not plans
        selected = client.post('/v1/chat', headers={**headers, 'Idempotency-Key': 'time-selected-1234'}, json=choice)
        assert selected.status_code == 200, selected.json()
        assert selected.json()['result_state'] == 'live_success'
        assert len(plans) == 1
        assert (plans[0].start, plans[0].end) == (date(2026, 9, 9), date(2026, 9, 10))


def test_anchor_survives_reopen_and_rotations_and_is_owner_bound(tmp_path, monkeypatch):
    now = [datetime(2026, 9, 10, 23, 59, tzinfo=ZONE).timestamp()]
    monkeypatch.setattr('app.chat_sessions.time.time', lambda: now[0])
    path = tmp_path / 'registry.sqlite3'
    store = ChatSessionStore(path)
    session = store.ensure_session(None, None)
    conversation = store.create_conversation(session)
    token = store.create_clarification_token(session, conversation, outbound_clarification('昨天出库量')['questions'],
                                             source_question='昨天出库量', source_reference_at=now[0] - 10)
    expected = now[0] - 10
    now[0] += 120
    reopened = ChatSessionStore(path)
    for _ in range(4):
        token = reopened.resume_clarification(session, conversation)['token']
        assert reopened.clarification_reference_time(session, conversation, token) == expected
    other = reopened.ensure_session(None, None)
    other_conversation = reopened.create_conversation(other)
    assert reopened.clarification_reference_time(other, other_conversation, token) is None
    with pytest.raises(ConversationNotFoundError):
        reopened.clarification_reference_time(other, conversation, token)


@pytest.mark.parametrize('clock_step', [0, -1])
def test_rotation_grace_retains_latest_issued_tokens(tmp_path, monkeypatch, clock_step):
    now = [datetime(2026, 9, 10, 23, 59, tzinfo=ZONE).timestamp()]
    monkeypatch.setattr('app.chat_sessions.time.time', lambda: now[0])
    store = ChatSessionStore(tmp_path / 'registry.sqlite3')
    session = store.ensure_session(None, None)
    conversation = store.create_conversation(session)
    anchor = now[0] - 60
    token = store.create_clarification_token(
        session, conversation, outbound_clarification('昨天出库量')['questions'],
        source_question='昨天出库量', source_reference_at=anchor,
    )
    draft_id = store.clarification_draft_id(session, conversation, token)
    # Descending hashes make hash-based eviction deterministically remove new tokens.
    values = sorted((f'rotation_{index:032d}' for index in range(6)),
                    key=lambda value: store._hash('ct_' + value), reverse=True)
    generated = iter(values)
    monkeypatch.setattr('app.chat_sessions.secrets.token_urlsafe', lambda _: next(generated))
    issued = [token]
    for _ in values:
        now[0] += clock_step
        resumed = store.resume_clarification(session, conversation)
        issued.append(resumed['token'])
        assert resumed['draft_id'] == draft_id
        for recent in issued[-3:]:
            assert store.clarification_reference_time(session, conversation, recent) == anchor
        for expired in issued[:-3]:
            assert store.clarification_reference_time(session, conversation, expired) is None
        with store._connection() as connection:
            count = connection.execute(
                f'SELECT COUNT(*) FROM {store._clarifications_table} '
                'WHERE principal_hash=? AND conversation_id=?',
                (session.principal_hash, conversation),
            ).fetchone()[0]
        assert count == min(3, len(issued))


@pytest.mark.parametrize('invalid', [float('inf'), float('nan'), -1, 0, True, '2026-09-10', 99999999999])
def test_invalid_source_anchor_rejected_before_token_creation(tmp_path, invalid):
    store = ChatSessionStore(tmp_path / 'registry.sqlite3')
    session = store.ensure_session(None, None)
    conversation = store.create_conversation(session)
    with pytest.raises(ClarificationValidationError):
        store.create_clarification_token(session, conversation, outbound_clarification('昨天出库量')['questions'], source_reference_at=invalid)
    assert not store.has_pending_clarification(session, conversation)


def test_legacy_schema_does_not_invent_an_original_request_time(tmp_path):
    path = tmp_path / 'registry.sqlite3'
    store = ChatSessionStore(path)
    session = store.ensure_session(None, None)
    conversation = store.create_conversation(session)
    token = store.create_clarification_token(session, conversation, outbound_clarification('昨天出库量')['questions'], source_question='昨天出库量')
    with store._connection() as connection:
        connection.execute(f'ALTER TABLE {store._clarifications_table} DROP COLUMN source_reference_at')
    reopened = ChatSessionStore(path)
    assert reopened.clarification_source_question(session, conversation, token) == '昨天出库量'
    assert reopened.clarification_reference_time(session, conversation, token) is None
    rotated = reopened.resume_clarification(session, conversation)['token']
    assert reopened.clarification_reference_time(session, conversation, rotated) is None
