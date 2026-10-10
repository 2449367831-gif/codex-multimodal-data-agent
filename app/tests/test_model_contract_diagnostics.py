"""Repair hints explain the failed contract without echoing private material."""

from copy import deepcopy
import json

import pytest

from app.codex_agent import _model_contract_repair_prompt, _model_query_plans
from app.tests.test_model_execution_states import execute, model_host, turn
from app.tests.test_model_query_contract import business, payload, validation


@pytest.mark.parametrize("change,code", [
    ({"kind": "metadata"}, "source_kind"),
    ({"kind": []}, "query_kind"),
    ({"result_columns": []}, "column_list"),
    ({"sql": "SELECT * FROM ywzt_ods.master_wms"}, "projection"),
    ({"sql": "SELECT wms_code AS location FROM ywzt_ods.master_wms"}, "projection"),
    ({"purpose": " "}, "purpose"),
    ({"sql": "DELETE FROM ywzt_ods.master_wms"}, "sql_shape"),
    ({"result_id": "private-invalid-id", "result_title": "仓库"}, "result_identity"),
])
def test_specific_contract_reason_without_private_plan(change, code):
    raw = json.dumps({**business(), **change})
    diagnostics = []
    assert _model_query_plans(raw, diagnostic_codes=diagnostics) is None
    assert diagnostics[-1] == code
    prompt = _model_contract_repair_prompt("当前请求", raw)
    assert "[HOST_CONTRACT_DIAGNOSTIC] " + code + ":" in prompt
    assert "ywzt_ods.master_wms" not in prompt
    assert "private-invalid-id" not in prompt
    assert "当前请求" in prompt


@pytest.mark.parametrize("mutation,code", [
    ({"unit": "private-unit-sentinel", "kind": "number", "label": "实发数量"}, "measure_unit"),
    ({"label": "private-label-sentinel"}, "column_identity"),
    ({"label": "周转天数×2"}, "column_identity"),
    ({"kind": "number", "label": "SKU编码"}, "identity_kind"),
])
def test_invalid_labels_or_units_are_not_repeated_to_the_model(mutation, code):
    plan = business()
    plan["result_columns"][0].update(mutation)
    prompt = _model_contract_repair_prompt("当前请求", json.dumps(plan))
    assert "[HOST_CONTRACT_DIAGNOSTIC] " + code + ":" in prompt
    assert "private-label-sentinel" not in prompt and "private-unit-sentinel" not in prompt
    assert "周转天数×2" not in prompt


def test_batch_member_error_is_not_mistaken_for_missing_tool_call():
    bad = {**validation(), "checks": []}
    prompt = _model_contract_repair_prompt("当前请求", json.dumps({"outcome": "query_plan_batch", "plans": [business(), bad]}))
    assert "[HOST_CONTRACT_DIAGNOSTIC] check_list:" in prompt


@pytest.mark.parametrize("mutation,code", [
    ({"plans": []}, "batch_shape"),
    ({"plans": [business(), business()]}, "batch_duplicate"),
    ({"plans": [{**business(), "kind": []}, validation()]}, "query_kind"),
    ({"private-extra": "private-sentinel"}, "batch_shape"),
])
def test_invalid_batch_shape_is_safe_and_diagnostic(mutation, code):
    plan = {"outcome": "query_plan_batch", "plans": [business(), validation()], **mutation}
    prompt = _model_contract_repair_prompt("当前请求", json.dumps(plan))
    assert "[HOST_CONTRACT_DIAGNOSTIC] " + code + ":" in prompt
    assert "private-sentinel" not in prompt


@pytest.mark.parametrize("initial", [True, False])
def test_initial_and_later_contract_repair_keep_context_and_execute_only_repaired_plan(model_host, initial):
    thread, bridge = model_host
    bad = deepcopy(business() if initial else validation())
    if initial:
        bad["result_columns"][0]["key"] = "missing"
        turns = [bad, business(), {"outcome": "completed", "answer": "仓库资料已查询"}]
        responses = [payload([{"location": "001", "label": "合成仓"}])]
        expected = "projection"
    else:
        bad["checks"] = []
        turns = [business(), bad, validation(), {"outcome": "completed", "answer": "仓库资料已查询"}]
        responses = [payload([{"location": "001", "label": "合成仓"}]), payload([{"conflicts": 0}])]
        expected = "check_list"
    thread.run.side_effect = [turn(value) for value in turns]
    bridge.query.side_effect = responses
    result = execute()
    assert result["query_error"] is None
    assert result["result_state"] == "live_success"
    assert result["thread_id"] == "offline-thread"
    assert bridge.query.call_count == len(responses)
    prompt = thread.run.call_args_list[1 if initial else 2].args[0]
    assert "[HOST_CONTRACT_DIAGNOSTIC] " + expected + ":" in prompt
    assert "查询仓库资料" in prompt


def test_valid_plan_does_not_emit_a_diagnostic():
    diagnostics = []
    assert _model_query_plans(json.dumps(business()), diagnostic_codes=diagnostics)
    assert diagnostics == []


def test_unknown_errors_never_echo_exception_text(monkeypatch):
    def reject(*args, **kwargs):
        raise ValueError("private-value-sentinel")
    monkeypatch.setattr("app.codex_agent.private_plan_options", reject)
    prompt = _model_contract_repair_prompt("当前请求", json.dumps(business()))
    assert "[HOST_CONTRACT_DIAGNOSTIC] unspecified:" in prompt
    assert "private-value-sentinel" not in prompt
