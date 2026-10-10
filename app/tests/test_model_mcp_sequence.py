"""Exercise the real request transport lifecycle under synthetic model turns."""

import pytest

from app.tests.test_mcp_request_session import bridge, transport
from app.tests.test_model_execution_states import execute, model_host, turn
from app.tests.test_model_multi_results import named, completed
from app.tests.test_model_query_contract import payload, validation


@pytest.mark.parametrize('invalid_first', [False, True])
def test_host_reuses_mcp_for_metadata_business_and_validation(model_host, transport, monkeypatch, invalid_first):
    thread, _ = model_host
    parent = bridge()
    monkeypatch.setattr('app.codex_agent.CodexMcpBridge', lambda **_: parent)
    meta = {'outcome':'query_plan', 'kind':'metadata', 'sql':'SELECT table_name FROM information_schema.tables', 'purpose':'schema'}
    turns = [(meta, payload([{'table_name':'fixture'}], ['table_name']))]
    if invalid_first:
        turns.append((named(), {
            'status': 'query_failed',
            'error': {
                'code': 'unknown_column',
                'message': 'field repair fixture',
                'result_state': 'failed',
            },
            'diagnostic': 'unknown_column',
        }))
    repaired = named()
    if invalid_first:
        repaired['sql'] += " WHERE wms_code <> ''"
    turns += [(repaired, payload([{'location':'001', 'label':'测试仓'}], ['location','label'])),
              (validation(), payload([{'conflicts':0}])),
              (completed(['directory']), None)]
    replies = iter(turns)

    def model_turn(*_args, **_kwargs):
        response, transport.reply = next(replies)
        return turn(response)

    thread.run.side_effect = model_turn
    result = execute()
    assert result['query_error'] is None and result['interaction']['outcome'] == 'completed'
    assert result['verified_business_results'][0]['rows'] == [{'location':'001', 'label':'测试仓'}]
    expected_sessions = 2 if invalid_first else 1
    assert len(transport.opened) == len(transport.initialized) == len(transport.closed) == expected_sessions
    assert len(transport.calls) == (4 if invalid_first else 3)
    assert transport.calls[-1][0] is transport.calls[-2][0]
    assert all(call[1] == 'query_doris_sql' for call in transport.calls)


def test_host_rejection_never_reopens_or_calls_model_to_bypass_policy(model_host, transport, monkeypatch):
    thread, _ = model_host
    parent = bridge()
    monkeypatch.setattr('app.codex_agent.CodexMcpBridge', lambda **_: parent)
    thread.run.return_value = turn(named())
    transport.reply = {'status':'failed', 'error':{'code':'sql_safety_error'}}
    result = execute()
    assert result['query_error']['code'] == 'query_rejected'
    assert thread.run.call_count == 1
    assert len(transport.calls) == len(transport.opened) == len(transport.closed) == 1
    assert not result.get('verified_business_results')


def test_clarification_does_not_open_mcp_process(model_host, transport, monkeypatch):
    thread, _ = model_host
    monkeypatch.setattr('app.codex_agent.CodexMcpBridge', lambda **_: bridge())
    thread.run.return_value = turn({'outcome':'clarification_required', 'questions':[]})
    result = execute()
    assert not result['query_execution']['live_business_result_executed']
    assert not transport.opened and not transport.calls
