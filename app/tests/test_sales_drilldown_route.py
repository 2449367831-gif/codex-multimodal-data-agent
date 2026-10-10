"""Runtime integration contract, established before enabling the route."""

from datetime import date, datetime
import json

import pytest

from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_drilldown import source, query
from app.tests.test_sales_aggregation import facts, candidate_scope_rules
from app.tests.test_metric_catalog import catalog_root, write_metric, write_release


QUESTION = '查询昨天成品销售出库数量 Top20，返回排名、SKU编码、SKU名称、实发数量、出库单数。用户点击其中一个 SKU 后，要能回钻到仓库、货主、客户、出库单号和出库时间明细。要求 Top20 汇总与明细回钻完全对账。'


@pytest.fixture
def approved(catalog_root, monkeypatch):
    for metric, version in [('sales.ranked_drilldown', '1.0.0'), ('sales.outbound_actual', '2.0.0')]:
        write_metric(catalog_root, f'{metric}.yaml', metric, version)
    monkeypatch.setattr('app.agent_core.planning.active_snapshot', lambda _: (catalog_root, 'fixture'))
    return catalog_root


def test_released_ranking_resolves_pinned_dependency_in_v2_file(approved):
    path = approved / 'metrics/sales.outbound_actual.yaml'
    path.rename(path.with_name('sales.outbound_actual.v2.yaml'))
    write_metric(approved, 'sales.outbound_actual.yaml', 'sales.outbound_actual', '1.0.0', state='pending_business_approval')
    write_release(approved, ['sales.ranked_drilldown@1.0.0', 'sales.outbound_actual@2.0.0'])
    plan = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(QUESTION)
    assert plan is not None and plan.route == 'sales_ranked_drilldown'


def test_approved_ranking_outside_manifest_is_not_routed(approved):
    write_release(approved, ['sales.outbound_actual@2.0.0'])
    assert RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(QUESTION) is None


class Reader:
    def __init__(self, db):
        self.db = db
        self.calls = []

    def query(self, *, sql, purpose):
        self.calls.append(sql)
        rows = query(self.db, sql)
        return {'status': 'succeeded', 'execution': {'executor_name': 'doris', 'is_mock': False,
                'rows': rows, 'row_count': len(rows), 'truncated': False}}


@pytest.fixture
def threaded_source(source):
    import sqlite3
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.deserialize(source.serialize(name='ywzt_ods'), name='ywzt_ods')
    yield db
    db.close()


def test_exact_requirement_routes_without_confusing_detail_columns_with_scope(approved):
    plan = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(QUESTION)
    assert plan and plan.route == 'sales_ranked_drilldown'
    assert (plan.start, plan.end, plan.top_n) == (date(2026, 9, 9), date(2026, 9, 10), 20)
    assert plan.scope.owner == plan.scope.channel == 'ALL'
    assert not plan.scope.warehouse_codes and not plan.scope.sku_codes
    assert plan.scope.finished_goods


@pytest.mark.parametrize('question', ['查询昨天成品销售出库数量Top20', '昨天成品销售出库Top5，返回排名、SKU编码、SKU名称、实发数量、出库单数'])
def test_ordinary_finished_goods_ranking_can_offer_drilldown_without_extra_instructions(approved, question):
    plan = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(question)
    assert plan.route == 'sales_ranked_drilldown'
    assert plan.top_n == (5 if 'Top5' in question else 20)


@pytest.mark.parametrize('suffix', ['仅客户001', '仅线上', '只要已付款', '排除云仓', '按订单创建时间', '含退货', '按2C/2B', '仅JYCK'])
def test_unknown_predicate_is_not_erased(approved, suffix):
    assert RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(QUESTION + suffix) is None


@pytest.mark.parametrize('ranking', ['Top0', 'Top101', 'Top1000'])
def test_invalid_top_size_never_becomes_a_different_ranking(approved, ranking):
    assert RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(QUESTION.replace('Top20', ranking)) is None


def test_exact_sku_case_and_new_question_scope_reset(approved):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    plan = planner.classify(QUESTION.replace('昨天', '昨天 HJXH 货主 SKU `AbC-123`'))
    assert plan.scope.sku_codes == ('AbC-123',) and plan.scope.owner == 'HJXH'
    assert planner.classify(QUESTION).scope.owner == 'ALL'
    assert planner.classify(QUESTION).scope.sku_codes == ()


@pytest.mark.parametrize('metric', ['sales.ranked_drilldown', 'sales.outbound_actual'])
def test_unpublished_dependency_cannot_enable_route(approved, metric):
    (approved / f'metrics/{metric}.yaml').write_text('status: candidate\napproval: {state: approved}\n', encoding='utf-8')
    assert RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(QUESTION) is None


def test_initial_runtime_returns_ranking_only_with_private_immutable_context(source, approved):
    reader = Reader(source)
    runtime = SemanticQueryRuntime(query_client=reader, clock=lambda: datetime(2026, 9, 10, 12))
    result = runtime.run_if_supported(QUESTION)
    assert result and result['result_state'] == 'live_success'
    assert len(result['business_table']['rows']) == 1
    assert result['business_table']['rows'][0]['quantity'] == '13.75'
    snapshot = result.ranking_snapshot
    assert snapshot.plan.top_n == 20 and snapshot.ranking[0].sku_code == 'A'
    assert 'ranking_snapshot' not in json.dumps(result)
    assert all("'detail' AS row_kind" not in sql for sql in reader.calls)
    detail = runtime.execute_ranked_sku(snapshot, 'A')
    assert detail['result_state'] == 'live_success'
    assert len(detail['business_table']['rows']) == 4
    assert detail['business_table']['complete_reconciled'] is True
    assert detail['query_execution']['live_business_result_executed'] is True
    assert detail['query_plan']['grain'] == 'owner+outbound+sku'
    assert snapshot.ranking[0].quantity.as_tuple() == result.ranking_snapshot.ranking[0].quantity.as_tuple()


def test_runtime_pages_reduce_scans_but_keep_two_complete_reads(source, approved):
    import sqlglot
    from app.agent_core.sales_drilldown import DRILLDOWN_PAGE_SIZE

    reader = Reader(source)
    runtime = SemanticQueryRuntime(query_client=reader, clock=lambda: datetime(2026, 9, 10, 12))
    ranking = runtime.run_if_supported(QUESTION)
    assert len(reader.calls) == 2
    detail = runtime.execute_ranked_sku(ranking.ranking_snapshot, 'A')
    assert len(reader.calls) == 4 and detail['business_table']['complete_reconciled'] is True
    assert DRILLDOWN_PAGE_SIZE == 5000
    assert all(int(sqlglot.parse_one(sql, read='doris').args['limit'].expression.this) == DRILLDOWN_PAGE_SIZE
               for sql in reader.calls)


def test_unknown_click_never_queries_and_changed_detail_preserves_original(source, approved):
    reader = Reader(source)
    runtime = SemanticQueryRuntime(query_client=reader, clock=lambda: datetime(2026, 9, 10, 12))
    initial = runtime.run_if_supported(QUESTION)
    calls = len(reader.calls)
    with pytest.raises(KeyError):
        runtime.execute_ranked_sku(initial.ranking_snapshot, 'not-ranked')
    assert len(reader.calls) == calls
    source.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=99 WHERE company_code='B'")
    with pytest.raises(SemanticRuntimeError) as failure:
        runtime.execute_ranked_sku(initial.ranking_snapshot, 'A')
    assert failure.value.reason == 'sales_drilldown_changed'
    assert initial['business_table']['rows'][0]['quantity'] == '13.75'


def test_click_after_midnight_keeps_original_day_and_rank(source, approved):
    source.execute("UPDATE ywzt_ods.master_sku SET sku_group='50'")
    now = [datetime(2026, 9, 10, 23, 59)]
    runtime = SemanticQueryRuntime(query_client=Reader(source), clock=lambda: now[0])
    initial = runtime.run_if_supported(QUESTION)
    now[0] = datetime(2026, 9, 11, 0, 1)
    detail = runtime.execute_ranked_sku(initial.ranking_snapshot, 'B')
    assert detail['query_plan']['start'] == '2026-09-09'
    assert detail['business_table']['summary'][0]['value'] == '4.25'
    assert '第2名' in detail['business_table']['caption']
    assert runtime.classify(QUESTION).start == date(2026, 9, 10)


@pytest.mark.parametrize('conflict', ['category', 'name'])
def test_runtime_does_not_rescope_away_master_conflicts(source, approved, conflict):
    source.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A',?,?)",
                   ('Conflicting name' if conflict == 'name' else '测试成品', '60' if conflict == 'category' else '50'))
    with pytest.raises(SemanticRuntimeError) as failure:
        SemanticQueryRuntime(query_client=Reader(source), clock=lambda: datetime(2026, 9, 10, 12)).run_if_supported(QUESTION)
    assert failure.value.reason == 'sales_ranking_invalid'


def test_empty_ranking_keeps_schema_but_has_no_click_targets(source, approved):
    source.execute('DELETE FROM ywzt_ods.scm_sales_shipment_detail')
    result = SemanticQueryRuntime(query_client=Reader(source), clock=lambda: datetime(2026, 9, 10, 12)).run_if_supported(QUESTION)
    assert result['result_state'] == 'no_data'
    assert len(result['business_table']['columns']) == 5 and not result['business_table']['rows']
    assert result.ranking_snapshot.ranking == ()


def test_original_requirement_is_preserved():
    from pathlib import Path
    cases = json.loads((Path(__file__).resolve().parents[2] / 'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))
    assert QUESTION == next(case['question'] for case in cases['cases'] if case['id'] == 'T38')


def task_result(client, headers, body):
    import time
    submitted = client.post('/v1/tasks', headers=headers, json=body)
    assert submitted.status_code == 202, submitted.json()
    task_id = submitted.json()['task_id']
    for _ in range(300):
        task = client.get(f'/v1/tasks/{task_id}').json()
        if task['state'] in {'completed', 'failed'}:
            return task
        time.sleep(0.02)
    pytest.fail('synthetic drilldown task did not finish')


def app_for(reader, tmp_path):
    from app.chat_api import create_chat_app, IdentityResolver
    from app.chat_sessions import ChatSessionStore
    from app.tests.test_chat_api import FakeChatRunner

    class NoModel(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError('ranked sales must not call model')

    return create_chat_app(NoModel(), semantic_runtime=SemanticQueryRuntime(query_client=reader,
        clock=lambda: datetime(2026, 9, 10, 12)), session_store=ChatSessionStore(tmp_path / 'sessions.sqlite3'),
        identity_resolver=IdentityResolver(mode='anonymous'), enforce_loopback=False)


def test_chat_ranking_click_isolation_replay_and_expiry(threaded_source, approved, tmp_path):
    from fastapi.testclient import TestClient
    reader = Reader(threaded_source)
    app = app_for(reader, tmp_path)
    with TestClient(app) as client, TestClient(app) as other:
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'ranking-request-001'}
        response = client.post('/v1/chat', headers=headers, json={'question': QUESTION, 'conversation_id': session['conversation_id']})
        assert response.status_code == 200, response.json()
        result = response.json()
        table = result['business_table']
        token = table['drilldown']
        assert set(token) == {'result_id', 'expires_at'}
        body = {'conversation_id': session['conversation_id'], 'drilldown': {'result_id': token['result_id'], 'sku_code': 'A'}}
        click_headers = {**headers, 'Idempotency-Key': 'ranking-click-001'}
        completed = task_result(client, click_headers, body)
        assert completed['http_status'] == 200
        detail = completed['result']['business_table']
        assert len(detail['rows']) == 4 and len(detail['columns']) == 8
        assert completed['result']['drilldown_source'] == body['drilldown']
        assert not any(key in completed['result'] for key in ('query_plan', 'sql', 'mcp_calls', 'ranking_snapshot'))
        calls = len(reader.calls)
        # Idempotent completion remains recoverable even after context expiry.
        app.state.ranking_contexts.clock = lambda: 10**12
        assert task_result(client, click_headers, body)['result'] == completed['result']
        assert len(reader.calls) == calls
        expired = task_result(client, {**headers, 'Idempotency-Key': 'ranking-click-expired'}, body)
        assert expired['http_status'] == 410 and len(reader.calls) == calls
        second = other.get('/v1/session').json()
        foreign = task_result(other, {'X-CSRF-Token': second['csrf_token'], 'Idempotency-Key': 'foreign-click-001'},
            {**body, 'conversation_id': second['conversation_id']})
        assert foreign['http_status'] == 404 and len(reader.calls) == calls
        assert other.get(f"/v1/tasks/{completed['task_id']}").status_code == 404
        assert client.post('/v1/tasks', json=body).status_code == 403


@pytest.mark.parametrize('extra', [{'question': 'different question'}, {'scope': {'owner': 'B'}}, {'drilldown': {'result_id': 'bad', 'sku_code': 'A', 'sql': 'SELECT 1'}}])
def test_click_cannot_carry_a_question_plan_or_sql(source, approved, tmp_path, extra):
    from fastapi.testclient import TestClient
    reader = Reader(source)
    with TestClient(app_for(reader, tmp_path)) as client:
        session = client.get('/v1/session').json()
        body = {'conversation_id': session['conversation_id'], 'drilldown': {'result_id': 'rr_' + 'a'*32, 'sku_code': 'A'}, **extra}
        response = client.post('/v1/tasks', headers={'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'invalid-click-001'}, json=body)
        assert response.status_code == 422
        assert not reader.calls


def add_detail_documents(db, *, unsafe=False):
    for i in range(105):
        code = f'PAGE-{i:03}'
        db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES ('HJXH',?,'W1','2026-09-09 12:00:00',700,'JYCK',?,'20',0,'false',?)",
                   (code, code, '<script>bad</script>' if unsafe and i == 104 else None))
        db.execute("INSERT INTO ywzt_ods.scm_sales_order_header VALUES ('HJXH',?,0,'false')", (code,))
        db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES ('HJXH',?,'A',1,0,'false')", (code,))


@pytest.mark.parametrize('unsafe', [False, True])
def test_every_detail_page_is_screened_before_any_page_is_published(threaded_source, approved, tmp_path, unsafe):
    from fastapi.testclient import TestClient
    add_detail_documents(threaded_source, unsafe=unsafe)
    reader = Reader(threaded_source)
    app = app_for(reader, tmp_path)
    with TestClient(app) as client, TestClient(app) as other:
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'all-pages-ranking'}
        rank = client.post('/v1/chat', headers=headers, json={'question': QUESTION, 'conversation_id': session['conversation_id']}).json()
        token = rank['business_table']['drilldown']
        task = task_result(client, {**headers, 'Idempotency-Key': 'all-pages-click'},
            {'conversation_id': session['conversation_id'], 'drilldown': {'result_id': token['result_id'], 'sku_code': 'A'}})
        if unsafe:
            assert task['http_status'] == 502
            assert 'business_table' not in task['result'] and '<script>' not in json.dumps(task)
            assert not app.state.result_pages._results
            return
        assert task['http_status'] == 200
        table = task['result']['business_table']
        assert len(table['rows']) == 100 and table['pagination']['total_rows'] == 109
        rid = table['pagination']['result_id']
        calls = len(reader.calls)
        page = client.get(f'/v1/results/{rid}/pages/2').json()['business_table']
        assert len(page['rows']) == 9 and len(reader.calls) == calls
        assert sum(row['customer_code'] is None for row in table['rows'] + page['rows']) == 105
        other.get('/v1/session')
        assert other.get(f'/v1/results/{rid}/pages/2').status_code == 404
        assert page['notes'] == table['notes']


def test_unknown_sku_and_reused_key_never_execute_another_query(threaded_source, approved, tmp_path):
    from fastapi.testclient import TestClient
    reader = Reader(threaded_source)
    app = app_for(reader, tmp_path)
    with TestClient(app) as client:
        session = client.get('/v1/session').json()
        headers = {'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'unknown-sku-ranking'}
        rank = client.post('/v1/chat', headers=headers, json={'question': QUESTION}).json()
        token = rank['business_table']['drilldown']
        body = {'conversation_id': session['conversation_id'], 'drilldown': {'result_id': token['result_id'], 'sku_code': 'NOT-RANKED'}}
        calls = len(reader.calls)
        click_headers = {**headers, 'Idempotency-Key': 'unknown-sku-click'}
        assert task_result(client, click_headers, body)['http_status'] == 400
        body['drilldown']['sku_code'] = 'A'
        assert client.post('/v1/tasks', headers=click_headers, json=body).status_code == 409
        assert len(reader.calls) == calls


def test_two_installations_keep_different_scopes_for_the_same_clicked_sku(threaded_source, approved, tmp_path):
    from fastapi.testclient import TestClient
    app = app_for(Reader(threaded_source), tmp_path)
    with TestClient(app) as first, TestClient(app) as second:
        selections = []
        for client, question in ((first, QUESTION.replace('昨天', '昨天HJXH货主')), (second, QUESTION)):
            session = client.get('/v1/session').json()
            headers = {'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'same-ranking-key'}
            response = client.post('/v1/chat', headers=headers, json={'question': question})
            assert response.status_code == 200
            token = response.json()['business_table']['drilldown']
            selections.append((client, {**headers, 'Idempotency-Key': 'same-click-key'},
                {'conversation_id': session['conversation_id'], 'drilldown': {'result_id': token['result_id'], 'sku_code': 'A'}}))
        results = [task_result(client, headers, body)['result']['business_table'] for client, headers, body in selections]
        assert results[0]['summary'][0]['value'] == '3.75'
        assert {row['cargo_owner'] for row in results[0]['rows']} == {'HJXH'}
        assert results[1]['summary'][0]['value'] == '13.75'
        assert {row['cargo_owner'] for row in results[1]['rows']} == {'HJXH', 'B'}
