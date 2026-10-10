from dataclasses import replace
from hashlib import sha256
import csv
import io
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.agent_core.sales_export import retrieve_export_chunk, summary_digest, position_digest
from app.agent_core.semantic_types import SemanticRuntimeError
from app.sales_exports import SalesExportStore, csv_bytes
from app.sales_detail_contexts import DetailContextError
from app.tests.test_sales_aggregation import facts
from app.tests.test_sales_detail_batches import initial
from app.tests.test_sales_detail_batch_api import application
from app.tests.test_sales_detail_chat import submit, build


def test_reconstructed_positions_preserve_whole_result_and_source_proofs(facts):
    cursor, query = initial(facts)
    rows, prior = [], None
    for offset in (0, 2, 4):
        batch = retrieve_export_chunk(cursor.plan, cursor.definition, summary_digest(cursor.baseline),
                                     offset, query, chunk_rows=2, previous_digest=prior)
        rows.extend(batch.rows)
        prior = position_digest(batch.cursor)
    assert len(rows) == 5 and batch.cursor.complete and batch.cursor.documents == 4
    assert batch.cursor.quantity == 18
    with pytest.raises(SemanticRuntimeError):
        retrieve_export_chunk(cursor.plan, cursor.definition, summary_digest(cursor.baseline),
                              2, query, chunk_rows=2, previous_digest='0'*64)


def test_export_store_restart_and_no_result_values_on_disk(facts, tmp_path):
    cursor, query = initial(facts)
    path = tmp_path / 'metadata.sqlite'
    store = SalesExportStore(path, chunk_rows=2)
    identifier = store.create('owner', 'conversation', 'source', cursor)
    item = store.get('owner', 'conversation', identifier)
    batch = retrieve_export_chunk(cursor.plan, cursor.definition, item['baseline'], 0, query, chunk_rows=2)
    encoded = b'PRIVATE_RESULT_ROW_SENTINEL'
    digest = store.record(item, 0, encoded, position_digest(batch.cursor))
    restarted = SalesExportStore(path, chunk_rows=50)
    restored = restarted.get('owner', 'conversation', identifier)
    assert restored['chunk_rows'] == 2
    assert restarted.position(restored, 1) == 2
    assert restarted.previous_digest(restored, 1) == position_digest(batch.cursor)
    assert b'PRIVATE_RESULT_ROW_SENTINEL' not in path.read_bytes()
    assert restarted.record(item, 0, encoded, position_digest(batch.cursor)) == digest
    with pytest.raises(DetailContextError):
        restarted.record(item, 0, b'changed', position_digest(batch.cursor))
    with pytest.raises(DetailContextError):
        restarted.get('another-owner', 'conversation', identifier)
    with pytest.raises(DetailContextError):
        restarted.get('owner', 'other-conversation', identifier)
    with pytest.raises(DetailContextError):
        restarted.position(item, 2)
    restarted.clock = lambda: item['expires'] + 1
    with pytest.raises(DetailContextError):
        restarted.get('owner', 'conversation', identifier)


def test_csv_neutralizes_formulas_and_has_one_header():
    row = dict(cargo_owner='=evil', warehouse_code='001', outbound_code='a,"b',
               sku_code='  +bad', ship_at='2026-09-09 12:00:00', quantity='1.234567890123456789')
    value = csv_bytes([row], first=True) + csv_bytes([row], first=False)
    decoded = list(csv.reader(io.StringIO(value.decode('utf-8-sig'))))
    assert len(decoded) == 3 and decoded[1][0] == "'=evil" and decoded[1][3] == "'  +bad"
    assert decoded[1][2] == 'a,"b' and decoded[1][-1] == '1.234567890123456789'


def test_csv_checks_byte_cap_before_retaining_an_oversized_chunk(monkeypatch):
    row = dict(cargo_owner='TEST',warehouse_code='W1',outbound_code='O1',sku_code='S1',
               ship_at='2026-09-09 12:00:00',quantity='1')
    one = csv_bytes([row], first=False)
    monkeypatch.setattr('app.sales_exports.MAX_EXPORT_BYTES', len(one))
    assert csv_bytes([row], first=False) == one
    with pytest.raises(DetailContextError) as error:
        csv_bytes([row,row], first=False)
    assert error.value.http_status == 413


def test_export_api_restarts_without_memory_cursors_or_saved_rows(application):
    app, query = application
    app.state.sales_exports.chunk_rows = 2
    with TestClient(app) as client, TestClient(app) as other:
        summary = submit(client).json()
        identifier = summary['business_table']['detail_batches']['export_id']
        meta = client.get('/v1/exports/' + identifier).json()
        assert meta['chunks'] == 3 and meta['total_rows'] == 5
        other.get('/v1/session')
        assert other.get('/v1/exports/' + identifier).status_code == 404
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token']}
        assert client.post(f'/v1/exports/{identifier}/chunks/2', headers=headers).status_code == 409
        assert client.post(f'/v1/exports/{identifier}/chunks/0', headers=headers, json={'sql': 'test'}).status_code == 422
        parts = []
        for index in range(3):
            part = client.post(f'/v1/exports/{identifier}/chunks/{index}', headers=headers)
            assert part.status_code == 200, part.text
            assert sha256(part.content).hexdigest() == part.headers['X-Chunk-SHA256']
            assert part.headers['X-Export-Complete'] == str(index == 2).lower()
            parts.append(part.content)
            calls = query.calls
            replay = client.post(f'/v1/exports/{identifier}/chunks/{index}', headers=headers)
            assert replay.content == part.content and query.calls == calls
            # Metadata survives losing all continuation/page memory.
            app.state.detail_contexts._contexts.clear()
        decoded = list(csv.reader(io.StringIO(b''.join(parts).decode('utf-8-sig'))))
        assert len(decoded) == 6 and sum(float(row[-1]) for row in decoded[1:]) == 18
        assert len({tuple(row[i] for i in (0, 2, 3)) for row in decoded[1:]}) == 5


def test_export_api_refuses_unscreened_and_changed_result(application, monkeypatch):
    app, _ = application
    app.state.sales_exports.chunk_rows = 2
    with TestClient(app) as client:
        identifier = submit(client).json()['business_table']['detail_batches']['export_id']
        headers = {'X-CSRF-Token': client.get('/v1/session').json()['csrf_token']}
        runtime = app.state.semantic_runtime
        original = runtime.execute_sales_export_chunk
        def unsafe(*args):
            batch, evidence = original(*args)
            return replace(batch, rows=({**batch.rows[0], 'sku_code': '<script>'}, *batch.rows[1:])), evidence
        monkeypatch.setattr(runtime, 'execute_sales_export_chunk', unsafe)
        response = client.post(f'/v1/exports/{identifier}/chunks/0', headers=headers)
        assert response.status_code == 409 and '<script>' not in response.text
        with sqlite3.connect(app.state.chat_session_store.path) as db:
            assert db.execute('SELECT COUNT(*) FROM sales_export_hashes_v1').fetchone()[0] == 0
        def changed(*args):
            raise SemanticRuntimeError('private error', reason='sales_detail_source_changed')
        monkeypatch.setattr(runtime, 'execute_sales_export_chunk', changed)
        response = client.post(f'/v1/exports/{identifier}/chunks/0', headers=headers)
        assert response.status_code == 409 and response.json()['error']['code'] == 'detail_source_changed'
        assert 'private error' not in response.text


def test_new_application_process_resumes_same_export(application, facts, tmp_path):
    app, _ = application
    app.state.sales_exports.chunk_rows = 2
    with TestClient(app) as client:
        identifier = submit(client).json()['business_table']['detail_batches']['export_id']
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token']}
        first = client.post(f'/v1/exports/{identifier}/chunks/0', headers=headers)
        assert first.status_code == 200
        cookies = dict(client.cookies)
    new_app, _ = build(facts, tmp_path)
    with TestClient(new_app) as client:
        client.cookies.update(cookies)
        meta = client.get('/v1/exports/' + identifier).json()
        assert meta['chunk_rows'] == 2
        replay = client.post(f'/v1/exports/{identifier}/chunks/0', headers=headers)
        assert replay.status_code == 200 and replay.content == first.content
        second = client.post(f'/v1/exports/{identifier}/chunks/1', headers=headers)
        assert second.status_code == 200 and second.headers['X-Loaded-Rows'] == '4'


def test_simultaneous_identical_export_reads_join_one_worker(application, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    app, _ = application
    app.state.sales_exports.chunk_rows = 2
    entered, release = threading.Event(), threading.Event()
    runtime = app.state.semantic_runtime
    original = runtime.execute_sales_export_chunk
    calls = []
    def delayed(*args):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(runtime, 'execute_sales_export_chunk', delayed)
    with TestClient(app) as client:
        identifier = submit(client).json()['business_table']['detail_batches']['export_id']
        headers = {'X-CSRF-Token': client.get('/v1/session').json()['csrf_token']}
        with ThreadPoolExecutor(2) as workers:
            first = workers.submit(client.post, f'/v1/exports/{identifier}/chunks/0', headers=headers)
            assert entered.wait(5)
            assert client.get('/health').json()['exports']['active'] == 1
            second = workers.submit(client.post, f'/v1/exports/{identifier}/chunks/0', headers=headers)
            release.set()
            left, right = first.result(), second.result()
        assert left.status_code == right.status_code == 200 and left.content == right.content
        assert calls == [1]


def test_export_pages_are_bounded_without_the_small_preview_limit(facts):
    from app.agent_core.sales_detail_batches import retrieve_sales_detail_batch
    from app.tests.test_sales_detail_partitions import query_fixture
    import sqlglot
    cursor, _ = initial(facts)
    query, _ = query_fixture(facts)
    limits = []
    def checked(sql, **kwargs):
        if 'result_ordinal' in sql:
            limits.append(int(sqlglot.parse_one(sql, read='doris').args['limit'].expression.this))
        return query(sql, **kwargs)
    batch = retrieve_sales_detail_batch(cursor, checked, max_rows=50000, batch_cap=50000)
    assert batch.cursor.complete and limits == [10000,10000]
