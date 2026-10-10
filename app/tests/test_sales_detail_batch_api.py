from functools import partial
import time

from fastapi.testclient import TestClient
import pytest

from app.agent_core.handlers import sales_details
from app.agent_core import runtime
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_aggregation import facts
from app.tests.test_sales_detail_chat import build, submit


@pytest.fixture
def application(facts, tmp_path, monkeypatch):
    monkeypatch.setattr(sales_details, 'retrieve_partitioned_sales_details',
                        partial(sales_details.retrieve_partitioned_sales_details, max_rows=1))
    monkeypatch.setattr(runtime, 'retrieve_sales_detail_batch', partial(runtime.retrieve_sales_detail_batch, max_rows=2))
    return build(facts, tmp_path)


def post_batch(client, meta, index, key):
    session = client.get('/v1/session').json()
    response = client.post('/v1/tasks', headers={'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': key},
        json={'conversation_id': session['conversation_id'], 'detail_batch': {'result_id': meta['result_id'], 'batch_index': index}})
    assert response.status_code == 202, response.json()
    task = response.json()
    deadline = time.monotonic() + 15
    while task['state'] in {'queued', 'running'} and time.monotonic() < deadline:
        time.sleep(.01)
        task = client.get('/v1/tasks/' + task['task_id']).json()
    assert task['state'] not in {'queued', 'running'}
    return task


def test_continue_whole_scope_with_exact_counts_and_replay(application):
    app, query = application
    with TestClient(app) as client:
        summary = submit(client).json()
        meta = summary['business_table']['detail_batches']
        assert meta['loaded_rows'] == 0 and meta['total_rows'] == 5 and meta['next_batch'] == 1
        rows = []
        for index, loaded in [(1, 2), (2, 4), (3, 5)]:
            task = post_batch(client, meta, index, f'batch-key-{index:020}')
            assert task['state'] == 'completed', task
            result = task['result']
            assert result['result_state'] == ('live_success' if index == 3 else 'batch_complete')
            if index == 3:
                from decimal import Decimal
                import re
                assert '共5行' in result['business_result']['text']
                assert Decimal(re.search(r'实发数量([\d.]+) EA', result['business_result']['text']).group(1)) == 18
                assert '4单' in result['business_result']['text']
            table = result['business_table']
            assert table['detail_batches']['loaded_rows'] == loaded
            assert table['detail_batches']['complete'] is (index == 3)
            assert '全部货主' in table['caption'] and '全部仓库' in table['caption']
            assert 'query_plan' not in result and 'rows' not in result
            rows.extend(table['rows'])
            calls = query.calls
            repeat = post_batch(client, meta, index, f'different-key-{index:020}')
            assert repeat['result'] == result and query.calls == calls
            same_key = post_batch(client, meta, index, f'batch-key-{index:020}')
            assert same_key['task_id'] == task['task_id'] and query.calls == calls
        assert len(rows) == 5
        assert len({(r['cargo_owner'], r['outbound_code'], r['sku_code']) for r in rows}) == 5
        assert post_batch(client, meta, 4, 'finished-batch-1234567890')['state'] == 'failed'


def test_other_installation_cannot_read_or_advance_cursor(application):
    app, query = application
    with TestClient(app) as client, TestClient(app) as other:
        meta = submit(client).json()['business_table']['detail_batches']
        calls = query.calls
        denied = post_batch(other, meta, 1, 'other-user-batch-1234567')
        assert denied['state'] == 'failed' and denied['http_status'] == 404
        assert query.calls == calls
        assert post_batch(client, meta, 2, 'skip-batch-1234567890123')['http_status'] == 409
        assert query.calls == calls


def test_failed_query_keeps_cursor_and_retry_can_complete(application, monkeypatch):
    app, query = application
    with TestClient(app) as client:
        meta = submit(client).json()['business_table']['detail_batches']
        original = runtime.retrieve_sales_detail_batch
        def fail(*args, **kwargs):
            raise SemanticRuntimeError('本批超时', reason='query_timeout')
        monkeypatch.setattr(runtime, 'retrieve_sales_detail_batch', fail)
        failed = post_batch(client, meta, 1, 'failing-batch-1234567890')
        assert failed['state'] == 'failed'
        item = app.state.detail_contexts._contexts[meta['result_id']]
        assert item.cursor.loaded == 0 and not item.receipts
        monkeypatch.setattr(runtime, 'retrieve_sales_detail_batch', original)
        assert post_batch(client, meta, 1, 'retry-batch-123456789012')['state'] == 'completed'


def test_source_change_and_expired_cursor_have_distinct_failures(application, monkeypatch):
    app, _ = application
    with TestClient(app) as client:
        meta = submit(client).json()['business_table']['detail_batches']
        def changed(*args, **kwargs):
            raise SemanticRuntimeError('changed', reason='sales_detail_source_changed')
        monkeypatch.setattr(runtime, 'retrieve_sales_detail_batch', changed)
        failure = post_batch(client, meta, 1, 'changed-batch-1234567890')
        assert failure['http_status'] == 409
        assert failure['result']['error']['code'] == 'detail_source_changed'
        app.state.detail_contexts.clock = lambda: meta['expires_at'] + 1
        assert post_batch(client, meta, 1, 'expired-batch-1234567890')['http_status'] == 410


def test_browser_cannot_supply_query_plan_or_override_scope(application):
    app, query = application
    with TestClient(app) as client:
        meta = submit(client).json()['business_table']['detail_batches']
        session = client.get('/v1/session').json()
        calls = query.calls
        for extra in ({'question': '昨天HJXH实际销售出库明细'}, {'scope': {'owner': 'HJXH'}}, {'drilldown': {}}):
            response = client.post('/v1/tasks', headers={'X-CSRF-Token': session['csrf_token']},
                json={'conversation_id': session['conversation_id'],
                      'detail_batch': {'result_id': meta['result_id'], 'batch_index': 1}, **extra})
            assert response.status_code >= 400
        assert query.calls == calls


def test_unsafe_display_never_commits_the_batch(application, monkeypatch):
    from dataclasses import replace
    app, _ = application
    original = runtime.retrieve_sales_detail_batch
    def unsafe(*args, **kwargs):
        batch = original(*args, **kwargs)
        rows = list(batch.rows)
        rows[-1] = {**rows[-1], 'sku_code': '<script>'}
        return replace(batch, rows=tuple(rows))
    monkeypatch.setattr(runtime, 'retrieve_sales_detail_batch', unsafe)
    with TestClient(app) as client:
        meta = submit(client).json()['business_table']['detail_batches']
        failure = post_batch(client, meta, 1, 'unsafe-batch-1234567890')
        assert failure['state'] == 'failed'
        item = app.state.detail_contexts._contexts[meta['result_id']]
        assert item.cursor.loaded == 0 and not item.receipts
        assert 'business_table' not in failure.get('result', {})
