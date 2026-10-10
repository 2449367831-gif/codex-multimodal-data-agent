from datetime import datetime, date
from zoneinfo import ZoneInfo
from copy import deepcopy
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest
import sqlglot

from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.sales_aggregation import SalesAggregation, compile_sales_aggregation
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_sales_aggregation import facts
from app.tests.test_outbound_clarification import QUESTION, answers


@pytest.mark.parametrize('values', [
    ('outbound_sales', 'outbound_actual_qty', 'outbound_shipped_at'),
    ('outbound_all', 'outbound_allocated_qty', 'outbound_reviewed_at'),
    ('outbound_sales', 'outbound_document_count', 'outbound_shipped_at'),
])
def test_choose_once_execute_or_explain_without_model(facts, tmp_path, values, monkeypatch):
    monkeypatch.setattr('app.chat_sessions.time.time', lambda: datetime(2026, 9, 10, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
    sql = compile_sales_aggregation(SalesAggregation(), start=date(2026, 9, 9), end=date(2026, 9, 10))
    fixture_rows = [dict(row) for row in facts.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
    class Query:
        calls = 0
        def query(self, *, sql, purpose):
            self.calls += 1
            rows = deepcopy(fixture_rows)
            return {'status': 'succeeded', 'execution': {'executor_name': 'doris', 'is_mock': False, 'rows': rows, 'row_count': len(rows), 'truncated': False}}
    class Runner(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError('reviewed clarification must not call the model')
    query = Query()
    app = create_chat_app(Runner(), semantic_runtime=SemanticQueryRuntime(query_client=query, clock=lambda: datetime(2026, 9, 10, 12)),
                          session_store=ChatSessionStore(tmp_path / 'chat.sqlite3'), identity_resolver=IdentityResolver(mode='anonymous'), enforce_loopback=False)
    with TestClient(app) as client, TestClient(app) as other:
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'outbound-initial-123456'}
        first = client.post('/v1/chat', headers=headers, json={'conversation_id': session['conversation_id'], 'question': QUESTION})
        assert first.status_code == 200, first.json()
        card = first.json()['clarification']
        assert card['required'] and len(card['questions']) == 3 and query.calls == 0
        choice = {'conversation_id': session['conversation_id'], 'question': '确认并继续', 'clarification_token': card['token'], 'clarification_answers': answers(values)}
        foreign = other.get('/v1/session').json()
        assert other.post('/v1/chat', headers={'X-CSRF-Token': foreign['csrf_token']}, json=choice).status_code >= 400
        incomplete = client.post('/v1/chat', headers={**headers, 'Idempotency-Key': 'outbound-incomplete-1234'}, json={**choice, 'clarification_answers': answers(values[:2])})
        assert incomplete.status_code >= 400 and query.calls == 0
        headers['Idempotency-Key'] = 'outbound-selected-123456'
        second = client.post('/v1/chat', headers=headers, json=choice)
        assert second.status_code == 200, second.json()
        result = second.json()
        assert not result['clarification']['required']
        if values in {
            ('outbound_sales', 'outbound_actual_qty', 'outbound_shipped_at'),
            ('outbound_sales', 'outbound_document_count', 'outbound_shipped_at'),
        }:
            assert result['result_state'] == 'live_success' and query.calls == 1
            table = result['business_table']
            assert table['rows'][0]['quantity'] == '18'
            assert table['rows'][0]['outbound_count'] == '4'
        else:
            assert result['status'] == 'blocked' and query.calls == 0
            assert result['result_state'] == 'business_definition_unpublished'
            assert 'business_table' not in result
            assert '无需重复选择' in json.dumps(result, ensure_ascii=False)
            assert '请重新提交完整问题' not in json.dumps(result, ensure_ascii=False)
        replay = client.post('/v1/chat', headers=headers, json=choice).json()
        assert replay['status'] == result['status']
        assert query.calls == int(values[0] == 'outbound_sales' and values[2] == 'outbound_shipped_at')


@pytest.mark.parametrize('measure', ['outbound_actual_qty', 'outbound_document_count'])
def test_scoped_selection_executes_scoped_sql_and_next_question_resets_scope(facts, tmp_path, monkeypatch, measure):
    current = [datetime(2026, 9, 10, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai'))]
    monkeypatch.setattr('app.chat_sessions.time.time', lambda: current[0].timestamp())
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET facility_code='10114790' WHERE facility_code='W1'")
    facts.execute("UPDATE ywzt_ods.master_wms SET wms_code='10114790' WHERE wms_code='W1'")
    fixture = facts.serialize(name='ywzt_ods')

    class Query:
        calls = 0

        def query(self, *, sql, purpose):
            self.calls += 1
            db = sqlite3.connect(':memory:')
            try:
                db.row_factory = sqlite3.Row
                db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
                db.deserialize(fixture, name='ywzt_ods')
                rows = [dict(row) for row in db.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
            finally:
                db.close()
            return {'status': 'succeeded', 'execution': {'executor_name': 'doris', 'is_mock': False,
                    'rows': rows, 'row_count': len(rows), 'truncated': False}}

    class Runner(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError('reviewed scoped selection must not call the model')

    query = Query()
    app = create_chat_app(Runner(), semantic_runtime=SemanticQueryRuntime(query_client=query, clock=lambda: current[0]),
                         session_store=ChatSessionStore(tmp_path / 'chat.sqlite3'),
                         identity_resolver=IdentityResolver(mode='anonymous'), enforce_loopback=False)
    with TestClient(app) as client, TestClient(app) as other:
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token']}
        body = {'conversation_id': session['conversation_id']}
        first = client.post('/v1/chat', headers=headers, json={**body, 'question': "昨天HJXH长沙黄兴SKU 'A'出库量，按仓库汇总"})
        assert first.status_code == 200, first.json()
        body['conversation_id'] = first.json()['conversation_id']
        card = first.json()['clarification']
        assert card['required'] and len(card['questions']) == 3 and query.calls == 0
        current[0] = datetime(2026, 9, 11, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        resumed = client.post('/v1/clarification/resume', headers=headers, json=body)
        assert resumed.status_code == 200, resumed.json()
        choice = {**body, 'clarification_token': resumed.json()['clarification']['token'],
                  'clarification_answers': answers(('outbound_sales', measure, 'outbound_shipped_at'))}
        foreign = other.get('/v1/session').json()
        assert other.post('/v1/chat', headers={'X-CSRF-Token': foreign['csrf_token']}, json=choice).status_code >= 400
        assert query.calls == 0
        selected_headers = {**headers, 'Idempotency-Key': 'scoped-outbound-selected-1234'}
        selected = client.post('/v1/chat', headers=selected_headers, json=choice)
        assert selected.status_code == 200, selected.json()
        result = selected.json()
        assert result['result_state'] == 'live_success' and not result['clarification']['required']
        row, = result['business_table']['rows']
        assert row['cargo_owner'] == 'HJXH' and row['warehouse_code'] == '10114790'
        assert row['quantity'] == '3.75' and row['outbound_count'] == '2'
        assert '2026-09-09' in result['business_table']['caption']
        assert query.calls == 1
        assert client.post('/v1/chat', headers=selected_headers, json=choice).json()['business_table'] == result['business_table']
        assert query.calls == 1
        following = client.post('/v1/chat', headers=headers, json={**body, 'question': '昨天出库量'}).json()
        next_result = client.post('/v1/chat', headers=headers, json={**body,
            'clarification_token': following['clarification']['token'],
            'clarification_answers': answers(('outbound_sales', measure, 'outbound_shipped_at'))})
        assert next_result.status_code == 200, next_result.json()
        table = next_result.json()['business_table']
        assert '2026-09-10' in table['caption']
        assert table['rows'][0]['quantity'] == '100' and table['rows'][0]['outbound_count'] == '1'
        assert query.calls == 2
