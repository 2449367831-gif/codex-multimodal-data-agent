from copy import deepcopy

import pytest

from app.agent_core.execution import QueryExecutionSession
from app.agent_core.semantic_types import SemanticRuntimeError
from app.codex_mcp_bridge import CodexMcpBridgeError
from app.query_scope import QueryScope, apply_scope
from app.tests.test_query_outcomes import payload


SQL = 'SELECT d.sku_code,d.ship_qty FROM ywzt_ods.scm_sales_shipment_detail d'


class Client:
    def __init__(self):
        self.calls = []
        self.error = None
        self.responses = [payload([{'synthetic': 1}]), payload([])]

    def query_many(self, *, statements, purpose, timeout_seconds):
        self.calls.append((statements, purpose, timeout_seconds))
        if self.error:
            raise self.error
        return deepcopy(self.responses)


def test_batch_preserves_each_statement_scope_and_counts_evidence():
    client = Client()
    for scope in (QueryScope(owner='HJXH', sku_codes=('001',)), QueryScope()):
        session = QueryExecutionSession(client, scope, 'HJXH')
        result = session.query_many([SQL, SQL], purpose='synthetic pages', timeout_seconds=1)
        statements, purpose, timeout = client.calls[-1]
        assert statements == [apply_scope(SQL, scope, legacy_owner='HJXH')]*2
        assert purpose == 'synthetic pages' and 0 < timeout <= 1
        assert result == [[{'synthetic': 1}], []]
        evidence = session.evidence()
        assert evidence['live_business_result_executed']
        assert evidence['query_call_count'] == evidence['successful_business_query_call_count'] == 2
        assert session.row_counts == [1, 0]


@pytest.mark.parametrize('reason', ['backend_rejected', 'query_failed', 'query_timeout', 'result_truncated'])
def test_batch_transport_failure_is_typed_without_success_evidence(reason):
    client = Client()
    client.error = CodexMcpBridgeError(reason)
    session = QueryExecutionSession(client, QueryScope(), 'HJXH')
    with pytest.raises(SemanticRuntimeError) as failure:
        session.query_many([SQL, SQL], purpose='synthetic failure', timeout_seconds=1)
    assert failure.value.reason == reason
    assert session.calls == 2 and session.succeeded == 0
    assert not session.evidence()['live_business_result_executed']


@pytest.mark.parametrize('mutation,reason', [({'is_mock':True},'not_live_result'), ({'truncated':True},'result_truncated'),
                                           ({'row_count':99},'row_count_mismatch')])
def test_later_invalid_payload_does_not_return_partial_batch(mutation, reason):
    client = Client()
    client.responses[-1]['execution'].update(mutation)
    session = QueryExecutionSession(client, QueryScope(), 'HJXH')
    with pytest.raises(SemanticRuntimeError) as failure:
        session.query_many([SQL, SQL], purpose='synthetic invalid page', timeout_seconds=1)
    assert failure.value.reason == reason
    assert not session.evidence()['live_business_result_executed']


def test_missing_batch_response_is_not_success():
    client = Client()
    client.responses.pop()
    session = QueryExecutionSession(client, QueryScope(), 'HJXH')
    with pytest.raises(SemanticRuntimeError):
        session.query_many([SQL, SQL], purpose='missing response', timeout_seconds=1)
    assert not session.evidence()['live_query_executed']


def test_legacy_client_uses_checked_sequential_fallback():
    class Legacy:
        def query(self, *, sql, purpose):
            return payload([{'sql_fixture': sql}])
    session = QueryExecutionSession(Legacy(), QueryScope(), 'HJXH')
    rows = session.query_many([SQL, SQL], purpose='legacy fixture', timeout_seconds=1)
    assert len(rows) == 2 and session.evidence()['successful_query_call_count'] == 2


def test_sequential_fallback_does_not_start_next_page_after_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('app.agent_core.execution.time.monotonic', lambda: now[0])
    class Legacy:
        def query(self, *, sql, purpose):
            now[0] += 2
            return payload([])
    session = QueryExecutionSession(Legacy(), QueryScope(), 'HJXH')
    with pytest.raises(SemanticRuntimeError) as failure:
        session.query_many([SQL, SQL], purpose='fallback timeout', timeout_seconds=1)
    assert failure.value.reason == 'query_timeout' and session.calls == 1


@pytest.mark.parametrize('statements,seconds', [([], 1), ([SQL]*5, 1), ([SQL], 0), ([SQL], float('nan'))])
def test_invalid_batch_never_starts_execution(statements, seconds):
    client = Client()
    session = QueryExecutionSession(client, QueryScope(), 'HJXH')
    with pytest.raises(ValueError):
        session.query_many(statements, purpose='invalid batch', timeout_seconds=seconds)
    assert not client.calls and session.calls == 0


def test_compiler_owned_scope_is_not_reapplied_to_diagnostic_dimensions():
    client = Client()
    session = QueryExecutionSession(client, QueryScope(finished_goods=True), 'HJXH')
    compiled = "SELECT sku_code,sku_group FROM ywzt_ods.master_sku"
    rows = session._query_many_checked([compiled, compiled], purpose='dimension controls', timeout_seconds=1)
    assert client.calls[0][0] == [compiled, compiled]
    assert rows == [[{'synthetic': 1}], []]
    assert session.evidence()['successful_business_query_call_count'] == 2


@pytest.mark.parametrize('reason', ['backend_rejected', 'query_failed', 'query_timeout', 'result_truncated'])
def test_compiler_owned_batch_preserves_backend_failures(reason):
    client = Client()
    client.error = CodexMcpBridgeError(reason)
    session = QueryExecutionSession(client, QueryScope(), 'HJXH')
    with pytest.raises(SemanticRuntimeError) as failure:
        session._query_many_checked([SQL, SQL], purpose='compiled controls', timeout_seconds=1)
    assert failure.value.reason == reason and not session.evidence()['live_business_result_executed']


def test_compiler_owned_batch_rejects_later_mock_and_missing_responses():
    for responses in ([payload([])], [payload([]), payload([])]):
        client = Client()
        client.responses = responses
        client.responses[-1]['execution']['is_mock'] = True
        session = QueryExecutionSession(client, QueryScope(), 'HJXH')
        with pytest.raises(SemanticRuntimeError):
            session._query_many_checked([SQL, SQL], purpose='compiled controls', timeout_seconds=1)
        assert not session.evidence()['live_business_result_executed']
