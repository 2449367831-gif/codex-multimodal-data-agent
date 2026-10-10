from datetime import datetime
from functools import partial
import sqlite3

from fastapi.testclient import TestClient
import pytest
import sqlglot

from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_sales_aggregation import facts


def build(facts, tmp_path):
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
                cursor = db.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])
                columns = [column[0] for column in cursor.description]
                rows = [dict(row) for row in cursor]
            finally:
                db.close()
            assert len(rows) <= 500
            return {'status': 'succeeded', 'execution': {'executor_name': 'doris', 'is_mock': False,
                    'columns': columns, 'rows': rows, 'row_count': len(rows), 'truncated': False}}

    class Runner(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError('explicit reviewed details must not call the model')

    query = Query()
    app = create_chat_app(Runner(), semantic_runtime=SemanticQueryRuntime(query_client=query, clock=lambda: datetime(2026, 9, 10, 12)),
                         session_store=ChatSessionStore(tmp_path / 'chat.sqlite3'),
                         identity_resolver=IdentityResolver(mode='anonymous'), enforce_loopback=False)
    return app, query


def submit(client, *, key='sales-details-request-1234'):
    session = client.get('/v1/session').json()
    return client.post('/v1/chat', headers={'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': key},
                       json={'conversation_id': session['conversation_id'], 'question': '昨天实际销售出库明细'})


@pytest.mark.parametrize('unsafe_tail', [False, True])
def test_complete_rows_paginate_only_after_every_page_is_screened(facts, tmp_path, unsafe_tail):
    records = [('TEST', f'PAGE-{i:04}', 'W1', '2026-09-09 12:00:00', 700, 'JYCK', f'ORDER-{i:04}', '20', 0, 'false') for i in range(600)]
    facts.executemany('INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES (?,?,?,?,?,?,?,?,?,?)', records)
    facts.executemany("INSERT INTO ywzt_ods.scm_sales_order_header VALUES (?,?,0,'false')", [('TEST', row[6]) for row in records])
    facts.executemany("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES (?,?,?,1,0,'false')",
                      [('TEST', row[1], '<script>' if unsafe_tail and i == 599 else 'A') for i, row in enumerate(records)])
    app, query = build(facts, tmp_path)
    with TestClient(app) as client, TestClient(app) as other:
        response = submit(client)
        if unsafe_tail:
            assert response.status_code >= 400
            assert not app.state.result_pages._results
            return
        assert response.status_code == 200, response.json()
        result = response.json()
        assert result['result_state'] == 'live_success'
        table = result['business_table']
        assert len(table['rows']) == 100
        assert table['pagination']['total_rows'] == 605 and table['pagination']['page_count'] == 7
        assert table['summary'][0]['value'] == '618'
        calls = query.calls
        url = f"/v1/results/{table['pagination']['result_id']}/pages/7"
        final_page = client.get(url)
        assert final_page.status_code == 200 and len(final_page.json()['business_table']['rows']) == 5
        other.get('/v1/session')
        assert other.get(url).status_code == 404
        assert query.calls == calls
        replay = submit(client)
        assert replay.json()['business_table']['pagination']['result_id'] == table['pagination']['result_id']
        assert query.calls == calls
        assert 'sales_detail_result' not in result and 'query_plan' not in result


def test_capacity_response_is_summary_only_not_complete_details(facts, tmp_path, monkeypatch):
    from app.agent_core.handlers import sales_details
    monkeypatch.setattr(sales_details, 'retrieve_partitioned_sales_details', partial(sales_details.retrieve_partitioned_sales_details, max_rows=1))
    app, query = build(facts, tmp_path)
    with TestClient(app) as client:
        response = submit(client)
        assert response.status_code == 200, response.json()
        result = response.json()
        assert result['result_state'] == 'summary_only'
        assert result['business_table']['rows'] == [{'quantity': '18', 'outbound_count': '4'}]
        assert '未' in result['business_result']['text']
        assert 'pagination' not in result['business_table']
        assert query.calls == 1


def test_successful_empty_detail_is_not_connection_failure(facts, tmp_path):
    facts.execute('DELETE FROM ywzt_ods.scm_sales_shipment_detail')
    app, _ = build(facts, tmp_path)
    with TestClient(app) as client:
        response = submit(client)
        assert response.status_code == 200, response.json()
        assert response.json()['result_state'] == 'no_data'
        assert response.json()['business_table']['rows'] == []
