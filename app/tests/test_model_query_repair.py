"""A repair must finish its original deliverable before resuming queued work."""

import pytest

from app.codex_mcp_bridge import CodexMcpBridgeError
from app.tests.test_model_execution_states import execute, model_host, turn
from app.tests.test_model_multi_results import completed, named, summary
from app.tests.test_model_query_contract import payload, validation
from app.tests.test_mcp_request_session import bridge as make_bridge, transport


def metadata():
    return {"outcome": "query_plan", "kind": "metadata", "purpose": "check schema",
            "sql": "SELECT column_name FROM information_schema.columns"}


def repaired_directory():
    plan = named()
    plan["sql"] += " WHERE wms_code IS NOT NULL"
    return plan


@pytest.mark.parametrize("batched_repair", [False, True])
@pytest.mark.parametrize("queued", [False, True])
def test_schema_repair_retains_failed_table_and_pending_validation(model_host, batched_repair, queued):
    thread, bridge = model_host
    initial = {"outcome": "query_plan_batch", "plans": [summary(), named(), validation()]} if queued else named()
    repair = {"outcome": "query_plan_batch", "plans": [metadata(), repaired_directory()]}
    final = completed() if queued else completed(["directory"])
    thread.run.side_effect = ([turn(initial), turn(repair), turn(final)] if batched_repair else
                              [turn(initial), turn(metadata()), turn(repaired_directory()), turn(final)])
    rows = [{"location": "001", "label": "测试仓"}]
    bridge.query.side_effect = ([payload([{"total": 1}])] if queued else []) + [
        CodexMcpBridgeError("query_failed", diagnostic="unknown_column"),
        payload([{"column_name": "wms_code"}]), payload(rows),
    ] + ([payload([{"conflicts": 0}])] if queued else [])
    result = execute()
    assert result["query_error"] is None
    assert result["interaction"]["outcome"] == "completed"
    assert result["verified_business_results"][-1]["rows"] == rows
    assert result["query_execution"]["metadata_query_call_count"] == 1
    assert result["query_execution"].get("validation_query_call_count", 0) == int(queued)
    assert bridge.query.call_count == (5 if queued else 3)
    assert thread.run.call_count == (3 if batched_repair else 4)
    if queued:
        assert bridge.query.call_args.kwargs["sql"] == validation()["sql"]
    if not batched_repair:
        assert "HOST_PENDING_QUERY_REPAIR" in thread.run.call_args_list[2].args[0]


@pytest.mark.parametrize("replacement", [metadata(), summary(), validation()])
def test_probe_or_other_table_cannot_erase_a_failed_deliverable(model_host, replacement):
    thread, bridge = model_host
    thread.run.side_effect = [turn(summary()), turn(named()), turn(replacement), turn(completed(["summary"]))]
    bridge.query.side_effect = [payload([{"total": 1}]), CodexMcpBridgeError("query_timeout"),
                                payload([{"column_name": "wms_code"}])]
    result = execute()
    assert result["interaction"]["outcome"] == "failed"
    assert result["query_error"]["code"] == "query_timeout"
    assert not result.get("verified_business_results")
    assert bridge.query.call_count == (3 if replacement["kind"] == "metadata" else 2)


@pytest.mark.parametrize("key,value", [
    ("result_id", "other"), ("result_title", "其他结果"),
    ("result_columns", [{"key": "location", "label": "别的编码"}, {"key": "label", "label": "别的名称"}]),
])
def test_single_query_repair_cannot_change_deliverable_contract(model_host, key, value):
    thread, bridge = model_host
    repair = {**repaired_directory(), key: value}
    thread.run.side_effect = [turn(named()), turn(repair), turn(completed([repair["result_id"]]))]
    bridge.query.side_effect = [CodexMcpBridgeError("query_failed"), payload([{"location": "001", "label": "测试仓"}])]
    result = execute()
    assert result["query_error"] is not None
    assert result["interaction"]["outcome"] == "failed"
    assert bridge.query.call_count == 1


def test_incomplete_model_turn_never_supplies_a_repair(model_host):
    thread, bridge = model_host
    incomplete = turn(repaired_directory())
    incomplete.status = "failed"
    thread.run.side_effect = [turn(named()), incomplete, turn(completed(["directory"]))]
    bridge.query.side_effect = [CodexMcpBridgeError("query_failed"), payload([{"location": "001", "label": "测试仓"}])]
    result = execute()
    assert result["interaction"]["outcome"] == "failed"
    assert bridge.query.call_count == 1


def test_schema_repair_reopens_mcp_and_keeps_validation(model_host, transport, monkeypatch):
    thread, _ = model_host
    parent = make_bridge()
    monkeypatch.setattr("app.codex_agent.CodexMcpBridge", lambda **_: parent)
    responses = iter([
        (named(), {"status": "failed", "error": {"code": "query_failed"}}),
        (metadata(), payload([{"column_name": "wms_code"}])),
        (repaired_directory(), payload([{"location": "001", "label": "测试仓"}])),
        (validation(), payload([{"conflicts": 0}])),
        (completed(["directory"]), None),
    ])

    def model_turn(*args, **kwargs):
        plan, transport.reply = next(responses)
        return turn(plan)

    thread.run.side_effect = model_turn
    result = execute()
    assert result["query_error"] is None
    assert result["interaction"]["outcome"] == "completed"
    assert len(transport.calls) == 4
    assert len(transport.opened) == len(transport.closed) == 2
    assert transport.calls[0][0] is not transport.calls[1][0]
    assert transport.calls[1][0] is transport.calls[2][0] is transport.calls[3][0]
    assert {call[1] for call in transport.calls} == {"query_doris_sql"}


def test_failed_probe_keeps_original_business_repair_prompt(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(named()), turn(metadata()), turn(repaired_directory()),
                              turn(completed(["directory"]))]
    bridge.query.side_effect = [CodexMcpBridgeError("query_failed"),
        CodexMcpBridgeError("backend_unavailable"), payload([{"location": "001", "label": "测试仓"}])]
    result = execute()
    assert result["interaction"]["outcome"] == "completed"
    assert result["query_error"] is None
    assert '"result_id": "directory"' in thread.run.call_args_list[2].args[0]
    assert bridge.query.call_count == 3


def test_validation_sql_repair_cannot_weaken_checks(model_host):
    thread, bridge = model_host
    relaxed = {**validation(), "sql": validation()["sql"] + " LIMIT 1",
               "checks": [{"column": "conflicts", "operator": "ge", "value": 0}]}
    thread.run.side_effect = [turn(named()), turn(validation()), turn(relaxed),
                              turn(completed(["directory"]))]
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]),
        CodexMcpBridgeError("query_failed"), payload([{"conflicts": 10}])]
    result = execute()
    assert result["interaction"]["outcome"] == "failed"
    assert result["query_error"] is not None
    assert bridge.query.call_count == 2
