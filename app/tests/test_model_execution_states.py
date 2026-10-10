"""Full model-host path with synthetic MCP payloads, without a model or DB call."""

from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.codex_agent import (
    _interaction_from_turn, _model_plan_first_prompt, _model_plan_fingerprint,
    _model_query_plan, _model_query_plans_from_turn,
    _model_request_text,
    _run_codex_turn, run_codex_question,
)
from app.codex_mcp_bridge import CodexMcpBridgeError


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def model_host(monkeypatch):
    client_class = MagicMock()
    monkeypatch.setattr("openai_codex.Codex", client_class)
    monkeypatch.setattr("openai_codex.CodexConfig", MagicMock())
    monkeypatch.setattr("app.codex_agent.assert_codex_environment_isolated", lambda: None)
    monkeypatch.setattr("app.codex_agent.isolated_codex_home", lambda *a, **kw: nullcontext(Path("C:/offline-model-home")))
    monkeypatch.setattr("app.codex_agent.codex_knowledge_context", lambda *a, **kw: "{}")
    thread = client_class.return_value.__enter__.return_value.thread_start.return_value
    thread.id = "offline-thread"
    bridge = MagicMock()
    bridge.query_sequence.side_effect = lambda: nullcontext(bridge)
    monkeypatch.setattr("app.codex_agent.CodexMcpBridge", lambda **_: bridge)
    return thread, bridge


def turn(value, items=None):
    return SimpleNamespace(id="offline-turn", status="completed", final_response=json.dumps(value), items=items or [], usage=None)


def execute():
    return run_codex_question("查询仓库资料", project_root=ROOT, backend_url="http://127.0.0.1:8010", model_provider="agent_gateway")


@pytest.mark.parametrize(
    "value",
    [
        "SELECT warehouse_name FROM ywzt_ods.master_wms",
        "```sql\nSELECT warehouse_name FROM ywzt_ods.master_wms\n```",
        '已完成理解： {"sql":"SELECT warehouse_name FROM ywzt_ods.master_wms","purpose":"warehouse lookup"}',
    ],
)
def test_private_plan_parser_accepts_gateway_wrappers_without_bypassing_validation(value):
    plan = _model_query_plan(value)
    assert plan is not None
    assert plan["sql"].startswith("SELECT")


def test_private_plan_parser_reads_commentary_when_final_message_is_generic():
    item = SimpleNamespace(
        root=SimpleNamespace(
            text=json.dumps({
                "outcome": "query_plan",
                "sql": "SELECT warehouse_name FROM ywzt_ods.master_wms",
                "purpose": "warehouse lookup",
            })
        )
    )
    result = SimpleNamespace(final_response="已开始处理，请稍候", items=[item])
    plans = _model_query_plans_from_turn(result)
    assert plans and plans[0]["sql"].startswith("SELECT")


def test_clarification_parser_reads_typed_commentary_when_final_message_is_generic():
    item = SimpleNamespace(
        root=SimpleNamespace(
            text=json.dumps({
                "outcome": "clarification_required",
                "questions": [{
                    "id": "time_window",
                    "prompt": "请选择时间范围",
                    "selection": "single",
                    "options": [{"id": "yesterday", "label": "昨天"}],
                }],
            })
        )
    )
    result = SimpleNamespace(final_response="已开始处理，请稍候", items=[item])
    assert _interaction_from_turn(result)["outcome"] == "clarification_required"


@pytest.mark.parametrize("reason,state", [("backend_rejected", "rejected"), ("query_failed", "failed"), ("result_truncated", "truncated")])
def test_failed_repair_preserves_typed_failure_and_cannot_turn_into_clarification(model_host, reason, state):
    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "offline"})
    bridge.query.side_effect = CodexMcpBridgeError(reason)
    result = execute()
    assert result["result_state"] == state
    assert result["query_error"]["result_state"] == state
    assert result["interaction"] == {"outcome": "failed", "questions": []}
    assert not result["query_execution"]["live_business_result_executed"]
    assert thread.run.call_count == (1 if reason in {"backend_rejected", "query_failed"} else 2)
    assert bridge.query.call_count == 1
    assert "sql" not in result["query_error"]


@pytest.mark.parametrize("reason,diagnostic", [("query_failed", "unknown_column"), ("result_truncated", None)])
def test_correctable_failure_can_replan_in_same_thread(model_host, reason, diagnostic):
    thread, bridge = model_host
    thread.run.side_effect = [
        turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"}),
        turn({"outcome": "query_plan", "sql": "SELECT DISTINCT wms_code FROM ywzt_ods.master_wms", "purpose": "repaired directory"}),
        turn({"outcome": "completed", "answer": "查询成功，当前范围没有匹配的仓库资料"}),
    ]
    bridge.query.side_effect = [
        CodexMcpBridgeError(reason, diagnostic=diagnostic),
        {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
            "truncated": False, "columns": [], "rows": [], "row_count": 0}},
    ]
    result = execute()
    assert result["thread_id"] == "offline-thread"
    assert result["result_state"] == "no_data"
    assert result["query_error"] is None
    assert result["host_failure"] is None
    assert bridge.query.call_count == 2
    assert thread.run.call_count == 3


def test_query_timeout_does_not_trigger_model_replan(model_host):
    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"})
    bridge.query.side_effect = CodexMcpBridgeError("query_timeout")
    result = execute()
    assert result["result_state"] == "failed"
    assert thread.run.call_count == 1
    assert bridge.query.call_count == 1


def test_query_unavailable_retries_once_with_identical_sql(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [
        turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"}),
        turn({"outcome": "completed", "answer": "查询成功，当前范围没有匹配的仓库资料"}),
    ]
    bridge.query.side_effect = [
        CodexMcpBridgeError("backend_unavailable"),
        {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
            "truncated": False, "columns": [], "rows": [], "row_count": 0}},
    ]
    result = execute()
    assert result["result_state"] == "no_data"
    assert result["query_error"] is None
    assert bridge.query.call_count == 2
    assert bridge.query.call_args_list[0].kwargs["sql"] == bridge.query.call_args_list[1].kwargs["sql"]
    assert thread.run.call_count == 2


def test_query_unavailable_stops_after_one_same_sql_retry(model_host):
    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"})
    bridge.query.side_effect = [
        CodexMcpBridgeError("backend_unavailable"),
        CodexMcpBridgeError("backend_unavailable"),
    ]
    result = execute()
    assert result["result_state"] == "failed"
    assert result["normalized_result_state"] == "query_unavailable"
    assert result["query_error"]["normalized_result_state"] == "query_unavailable"
    assert bridge.query.call_count == 2
    assert thread.run.call_count == 1


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT table_name FROM information_schema.tables"])
def test_bridge_metadata_probe_is_not_business_result(model_host, sql):
    thread, bridge = model_host
    thread.run.side_effect = [
        turn({"outcome": "query_plan", "sql": sql, "purpose": "probe"}),
        turn({"outcome": "blocked", "questions": []}),
    ]
    bridge.query.return_value = {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "truncated": False, "columns": ["test"], "rows": [{"test": 1}], "row_count": 1}}
    result = execute()
    assert not result["query_execution"]["live_business_result_executed"]
    assert result["query_execution"]["metadata_query_call_count"] == 1


def test_empty_business_response_has_explicit_no_data_state(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [
        turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "offline"}),
        turn({"outcome": "completed", "answer": "查询成功，当前范围没有匹配的仓库资料"}),
    ]
    bridge.query.return_value = {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "truncated": False, "columns": [], "rows": [], "row_count": 0}}
    result = execute()
    assert result["result_state"] == "no_data"
    assert result["query_execution"]["result_state"] == "no_data"


def test_metadata_then_business_queries_use_one_thread_and_count_only_business(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [
        turn({"outcome": "query_plan", "sql": "SELECT column_name FROM information_schema.columns", "purpose": "schema"}),
        turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"}),
        turn({"outcome": "completed", "answer": "查询成功，当前范围没有匹配的仓库资料"}),
    ]
    bridge.query.side_effect = [
        {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "truncated": False, "columns": ["column_name"], "rows": [{"column_name": "wms_code"}], "row_count": 1}},
        {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "truncated": False, "columns": [], "rows": [], "row_count": 0}},
    ]
    result = execute()
    assert result["thread_id"] == "offline-thread"
    assert result["result_state"] == "no_data"
    assert result["query_execution"]["query_call_count"] == 2
    assert result["query_execution"]["metadata_query_call_count"] == 1
    assert result["query_execution"]["successful_business_query_call_count"] == 1
    assert thread.run.call_count == 3


def test_repeating_same_query_plan_does_not_loop_or_publish_partial_result(model_host):
    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"})
    bridge.query.return_value = {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "truncated": False, "columns": [], "rows": [], "row_count": 0}}
    result = execute()
    assert result["result_state"] == "failed"
    assert result["interaction"]["outcome"] == "failed"
    assert bridge.query.call_count == 1
    assert result["host_failure"] == {"stage": "repeated_plan", "step": 3}
    assert thread.run.call_count == 3


def test_long_memory_never_hides_current_question(model_host):
    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "clarification_required", "questions": []})
    question = "查询当前请求专属的仓库资料，完整保留这个条件"
    run_codex_question(question, project_root=ROOT, model_provider="agent_gateway",
                       request_context=[{"question": "历史" * 3000}])
    prompt = thread.run.call_args.args[0]
    assert question in prompt
    assert prompt.index(question) < prompt.index("LEARNING_SUMMARY")
    assert "历史" * 3000 in prompt
    bridge.query.assert_not_called()


def test_internal_capsule_is_not_subject_to_public_question_limit():
    capsule = "CURRENT_USER_REQUEST: current\nLEARNING_SUMMARY: " + "historical " * 1000
    assert capsule.strip() in _model_plan_first_prompt(capsule)
    with pytest.raises(ValueError):
        _model_request_text("x" * 32001)


@pytest.mark.parametrize("repair", [False, True])
def test_recovery_keeps_original_request_and_selected_choices(model_host, repair):
    thread, bridge = model_host
    plan = turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"})
    thread.run.side_effect = [plan if repair else turn({"outcome": "failed"}),
                             turn({"outcome": "blocked"})]
    bridge.query.side_effect = CodexMcpBridgeError("query_failed", diagnostic="unknown_column")
    original = "查询当前明确选定范围的仓库资料"
    run_codex_question("继续", project_root=ROOT, model_provider="agent_gateway",
        original_question=original,
        ui_selections=[{"question_id": "scope", "option_ids": ["all_warehouse"],
                        "prompt": "请选择仓库范围",
                        "selected_options": [{"id": "all_warehouse", "label": "全部仓库"}]}])
    # Invalid repair output gets one bounded JSON-contract correction after
    # the query repair turn; selected choices remain in every turn.
    assert thread.run.call_count == (3 if repair else 3)
    for call in thread.run.call_args_list:
        assert original in call.args[0]
        assert "all_warehouse" in call.args[0]
        assert "全部仓库" in call.args[0]
        assert "请选择仓库范围" in call.args[0]


@pytest.mark.parametrize("effort,expected", [("xhigh", "high"), ("high", "high"), ("medium", "medium")])
def test_transport_retry_exists_at_every_reasoning_level(monkeypatch, effort, expected):
    from openai_codex import Sandbox
    from openai_codex.generated.v2_all import ReasoningEffort
    monkeypatch.setattr("app.codex_agent.time.sleep", lambda _: None)
    thread = MagicMock()
    sentinel = object()
    thread.run.side_effect = [TimeoutError(), sentinel]
    assert _run_codex_turn(thread, "query", effort=ReasoningEffort(effort),
                           model="gpt-5.6-sol", sandbox=Sandbox.read_only) is sentinel
    assert thread.run.call_count == 2
    assert thread.run.call_args.kwargs["effort"] == ReasoningEffort(expected)


def test_plan_identity_ignores_formatting_but_not_values():
    assert _model_plan_fingerprint("select wms_code from ywzt_ods.master_wms") == _model_plan_fingerprint(
        "SELECT /* regenerated */ wms_code\nFROM ywzt_ods.master_wms")
    assert _model_plan_fingerprint("SELECT 'A' FROM ywzt_ods.master_wms") != _model_plan_fingerprint(
        "SELECT 'a' FROM ywzt_ods.master_wms")


def test_repair_budget_is_independent_of_prior_metadata_steps(model_host):
    thread, bridge = model_host
    sqls = [
        "SELECT table_name FROM information_schema.tables",
        "SELECT column_name FROM information_schema.columns",
        "SELECT data_type FROM information_schema.columns",
        "SELECT wms_code FROM ywzt_ods.master_wms",
        "SELECT DISTINCT wms_code FROM ywzt_ods.master_wms",
    ]
    thread.run.side_effect = [turn({"outcome": "query_plan", "sql": sql, "purpose": "offline"}) for sql in sqls] + [
        turn({"outcome": "completed", "answer": "查询成功，没有匹配仓库"})]
    payload = {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
        "truncated": False, "columns": [], "rows": [], "row_count": 0}}
    bridge.query.side_effect = [payload, payload, payload, CodexMcpBridgeError("query_failed", diagnostic="unknown_column"), payload]
    result = execute()
    assert result["result_state"] == "no_data"
    assert result["query_error"] is None
    assert bridge.query.call_count == 5
    assert result["query_execution"]["metadata_query_call_count"] == 3


def test_model_receives_safe_failure_diagnostic(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [
        turn({"outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "directory"}),
        turn({"outcome": "blocked"}),
    ]
    bridge.query.side_effect = CodexMcpBridgeError("query_failed", diagnostic="unknown_column")
    result = execute()
    assert "(unknown_column)" in thread.run.call_args.args[0]
    assert "diagnostic" not in result["query_error"]
    assert result["host_failure"] == {"stage": "query", "diagnostic": "unknown_column", "step": 1}
    assert result["result_state"] == "failed"
