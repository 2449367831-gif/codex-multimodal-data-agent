"""Projection identity and reconciliation stay host-owned across model steps."""

from copy import deepcopy
import json

import pytest

from app.codex_agent import _model_query_plan
from app.chat_api import _public_model_verified_table
from app.model_query_contract import validate_business_projection, validate_reconciliation
from app.query_outcomes import DorisResultError
from app.tests.test_model_execution_states import model_host, execute, turn


def business(sql="SELECT wms_code AS location, wms_name AS label FROM ywzt_ods.master_wms"):
    return {"outcome": "query_plan", "sql": sql, "purpose": "directory",
            "kind": "business", "result_columns": [
                {"key": "location", "label": "仓库编码"}, {"key": "label", "label": "仓库名称"}]}


def validation():
    return {"outcome": "query_plan", "kind": "validation",
            "sql": "SELECT COUNT(*) AS conflicts FROM ywzt_ods.master_wms WHERE wms_code IS NULL",
            "purpose": "directory reconciliation",
            "checks": [{"column": "conflicts", "operator": "eq", "value": 0}]}


def payload(rows, columns=None):
    return {"status": "succeeded", "execution": {"columns": columns or list(rows[0] if rows else []),
        "rows": rows, "row_count": len(rows), "truncated": False, "is_mock": False, "executor_name": "doris"}}


def test_business_projection_survives_successful_validation(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(business()), turn(validation()), turn({"outcome": "completed", "answer": "已查询仓库资料"})]
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]), payload([{"conflicts": 0}])]
    result = execute()
    assert result["result_state"] == "live_success"
    assert result["verified_business_result"]["rows"] == [{"location": "001", "label": "测试仓"}]
    assert result["verified_business_result"]["result_columns"] == business()["result_columns"]
    assert result["query_execution"]["successful_business_query_call_count"] == 1
    assert result["query_execution"]["validation_query_call_count"] == 1
    from app.chat_api import _query_execution_evidence
    normalized = _query_execution_evidence(result["mcp_calls"], result["query_execution"])
    assert normalized["status"] == "live_result"
    assert normalized["successful_business_query_call_count"] == normalized["validation_query_call_count"] == 1
    assert thread.run.call_count == 3 and bridge.query.call_count == 2
    assert all(set(call.kwargs) == {"sql", "purpose"} for call in bridge.query.call_args_list)
    for call in thread.run.call_args_list[1:]:
        assert "CURRENT_REQUEST_CAPSULE:\n查询仓库资料\nEND_CURRENT_REQUEST_CAPSULE" in call.args[0]


@pytest.mark.parametrize("rows", [[{"conflicts": 1}], [{"conflicts": None}], [{"conflicts": "NaN"}], [], [{"other": 0}]])
def test_failed_validation_cannot_publish_earlier_data_or_retry_away_conflict(model_host, rows):
    thread, bridge = model_host
    thread.run.side_effect = [turn(business()), turn(validation())]
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]), payload(rows)]
    result = execute()
    assert result["result_state"] == "live_success"
    assert result["interaction"]["outcome"] == "completed"
    assert result["query_error"] is None
    assert result["validation_warnings"][0]["code"] == "reconciliation_failed"
    assert thread.run.call_count == 2


def test_validation_without_business_data_cannot_claim_answer(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(validation()), turn({"outcome": "completed", "answer": "已完成"})]
    bridge.query.return_value = payload([{"conflicts": 0}])
    result = execute()
    assert result["result_state"] == "failed"
    assert result["query_error"]["code"] == "query_failed"
    assert not result["query_execution"]["live_business_result_executed"]
    assert result["verified_business_result"] is None
    from app.chat_api import _query_execution_evidence
    normalized = _query_execution_evidence(result["mcp_calls"], result["query_execution"])
    assert normalized["status"] == "validation_only"
    assert not normalized["live_business_result_executed"]


@pytest.mark.parametrize("rows", [[], [{"location": "001", "label": "测试仓"}]])
def test_reconciliation_warning_keeps_evidence_consistent_across_runner(model_host, tmp_path, monkeypatch, rows):
    import subprocess
    from app.chat_api import ChatRunner, _query_execution_evidence

    thread, bridge = model_host
    thread.run.side_effect = [turn(business()), turn(validation()),
                            turn({"outcome": "completed", "answer": "当前条件下没有匹配资料"})]
    bridge.query.side_effect = [payload(rows, ["location", "label"]), payload([{"conflicts": 1}])]
    result = execute()
    evidence = _query_execution_evidence(result["mcp_calls"], result["query_execution"])
    assert evidence["status"] == "live_result"
    assert evidence["successful_business_query_call_count"] == 1
    assert evidence["failed_query_call_count"] == (1 if rows else 0)
    assert evidence.get("validation_query_call_count", 0) == 0
    assert evidence["normalized_result_state"] == ("succeeded" if rows else "empty_result")

    runner = ChatRunner(project_root=tmp_path)
    monkeypatch.setattr(ChatRunner, "auth_configured", property(lambda _: True))

    def child(command, **kwargs):
        envelope = {"nonce": kwargs["env"]["AGENT_CHAT_INTERNAL_RESULT_NONCE"], "result": result}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    received = runner.run("查询仓库资料")
    assert received["verified_business_result"]["rows"] == rows
    if rows:
        assert received["validation_warnings"][0]["code"] == "reconciliation_failed"
    else:
        assert not received["validation_warnings"]


def test_later_empty_business_answer_does_not_inherit_lookup_success(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(business()), turn(business("SELECT wms_code AS location, wms_name AS label FROM ywzt_ods.master_wms WHERE wms_code='absent'")),
                             turn({"outcome": "completed", "answer": "当前范围没有匹配资料"})]
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]), payload([], ["location", "label"])]
    result = execute()
    assert result["result_state"] == result["query_execution"]["result_state"] == "no_data"
    assert result["verified_business_result"]["rows"] == []


def test_validation_after_empty_business_does_not_turn_zero_rows_into_live_success(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(business()), turn(validation()), turn({"outcome": "completed", "answer": "没有匹配资料"})]
    bridge.query.side_effect = [payload([], ["location", "label"]), payload([{"conflicts": 0}])]
    result = execute()
    assert result["result_state"] == "no_data"
    assert result["verified_business_result"]["rows"] == []


@pytest.mark.parametrize("rows,columns", [
    ([{"location": "001"}], ["location", "label"]),
    ([{"location": "001", "label": "测试仓"}], ["location", "location"]),
    ([{"location": "001", "label": "测试仓", "extra": 1}], ["location", "label"]),
    ([], ["location"]),
])
def test_result_shape_must_match_declared_projection(model_host, rows, columns):
    thread, bridge = model_host
    thread.run.return_value = turn(business())
    bridge.query.return_value = payload(rows, columns)
    result = execute()
    assert result["result_state"] == "failed"
    assert result["verified_business_result"] is None
    assert thread.run.call_count == 1


def test_invalid_next_plan_cannot_publish_previous_partial_table(model_host):
    thread, bridge = model_host
    bad = business()
    bad["result_columns"][0]["key"] = "absent"
    thread.run.side_effect = [turn(business()), turn(bad), turn(bad)]
    bridge.query.return_value = payload([{"location": "001", "label": "测试仓"}])
    result = execute()
    assert result["result_state"] == "failed"
    assert result["interaction"]["outcome"] == "failed"
    assert bridge.query.call_count == 1
    assert thread.run.call_count == 3


def test_next_plan_contract_can_be_repaired_without_rerunning_business_query(model_host):
    thread, bridge = model_host
    invalid = {**validation(), "checks": []}
    thread.run.side_effect = [turn(business()), turn(invalid), turn(validation()),
                             turn({"outcome": "completed", "answer": "已查询仓库资料并核对"})]
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]), payload([{"conflicts": 0}])]
    result = execute()
    assert result["result_state"] == "live_success"
    assert result["verified_business_result"]["rows"] == [{"location": "001", "label": "测试仓"}]
    assert bridge.query.call_count == 2 and thread.run.call_count == 4
    assert "HOST_CONTRACT_REPAIR" in thread.run.call_args_list[2].args[0]
    assert "查询仓库资料" in thread.run.call_args_list[2].args[0]


@pytest.mark.parametrize("mutation", [
    {"kind": "metadata"}, {"kind": "other"}, {"kind": []},
    {"result_columns": []}, {"result_columns": [{"key": "location", "label": "仓库编码"}]},
    {"checks": []}, {"sql": "SELECT * FROM ywzt_ods.master_wms"},
    {"sql": "SELECT wms_code AS location, wms_name AS location FROM ywzt_ods.master_wms"},
])
def test_invalid_contract_is_not_an_executable_plan(mutation):
    plan = {**business(), **mutation}
    assert _model_query_plan(json.dumps(plan)) is None


@pytest.mark.parametrize("column", [
    {"key": "location", "label": "仓库编码", "kind": "text"},
    {"key": "qty", "label": "数量", "kind": "number", "unit": "EA"},
])
def test_business_projection_can_declare_type_and_confirmed_unit(column):
    plan = business("SELECT wms_code AS location, wms_code AS qty FROM ywzt_ods.master_wms")
    plan["result_columns"] = [column, {"key": "label", "label": "仓库名称", "kind": "text"}]
    if column["key"] == "location":
        plan["sql"] = "SELECT wms_code AS location, wms_name AS label FROM ywzt_ods.master_wms"
    else:
        plan["sql"] = "SELECT wms_code AS qty, wms_name AS label FROM ywzt_ods.master_wms"
        plan["result_columns"] = [column, {"key": "label", "label": "仓库名称", "kind": "text"}]
    assert _model_query_plan(json.dumps(plan)) is not None


@pytest.mark.parametrize("column", [
    {"key": "qty", "label": "数量", "kind": "number", "unit": "unconfirmed_code"},
    {"key": "qty", "label": "数量", "kind": "number", "unit": "EA", "extra": 1},
    {"key": "qty", "label": "数量", "kind": "float"},
    {"key": "code", "label": "编码", "kind": "number"},
])
def test_business_projection_rejects_uncontrolled_type_or_unit(column):
    plan = business("SELECT wms_code AS code, wms_name AS label FROM ywzt_ods.master_wms")
    plan["result_columns"] = [column, {"key": "label", "label": "仓库名称", "kind": "text"}]
    plan["sql"] = f"SELECT wms_code AS {column['key']}, wms_name AS label FROM ywzt_ods.master_wms"
    assert _model_query_plan(json.dumps(plan)) is None


def test_numeric_measure_accepts_high_precision_string_values():
    plan = business("SELECT CAST(1.2300 AS DECIMAL(20, 4)) AS qty, wms_name AS label FROM ywzt_ods.master_wms")
    plan["result_columns"] = [
        {"key": "qty", "label": "实发数量", "kind": "number", "unit": "EA"},
        {"key": "label", "label": "仓库名称", "kind": "text"},
    ]
    parsed = _model_query_plan(json.dumps(plan))
    assert parsed is not None and parsed["result_columns"][0]["unit"] == "EA"


@pytest.mark.parametrize("label", ["数据库密码", "h.wms_code", "系统指令", "SELECT 值", "location", "仓库<script>", None])
def test_private_or_invalid_labels_are_rejected(label):
    plan = business()
    plan["result_columns"][0]["label"] = label
    assert _model_query_plan(json.dumps(plan)) is None


def test_contract_labels_bind_by_key_not_order_or_user_wording():
    data = {"columns": ["label", "location"], "rows": [{"location": "001", "label": "测试仓"}],
            "result_columns": business()["result_columns"]}
    validate_business_projection(data, data["result_columns"])
    table = _public_model_verified_table(data)
    assert {c["label"]: table["rows"][0][c["key"]] for c in table["columns"]} == {"仓库名称": "测试仓", "仓库编码": "001"}
    assert all("source_key" not in column for column in table["columns"])
    assert "result_columns" not in table
    changed = deepcopy(data)
    changed["result_columns"][0]["key"] = "missing"
    assert _public_model_verified_table(changed) is None


def test_host_hidden_columns_cannot_be_exposed_by_a_display_label():
    table = _public_model_verified_table({"columns": ["validation_status", "location"],
        "rows": [{"validation_status": "private", "location": "001"}], "result_columns": [
            {"key": "validation_status", "label": "仓库数量"}, {"key": "location", "label": "仓库编码"}]})
    assert [c["label"] for c in table["columns"]] == ["仓库编码"]
    assert table["rows"] == [{"col_1": "001"}]


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "invalid"])
def test_reconciliation_does_not_coerce_missing_or_invalid_values(value):
    with pytest.raises(DorisResultError):
        validate_reconciliation([{"conflicts": value}], validation()["checks"])


def test_all_rows_must_pass_not_only_the_first():
    with pytest.raises(DorisResultError):
        validate_reconciliation([{"conflicts": 0}, {"conflicts": 2}], validation()["checks"])


def test_declared_inventory_rule_has_fixed_safe_explanation_without_values():
    from app.query_outcomes import backend_error
    plan = validation()
    plan["checks"][0]["rule"] = "inventory_net_nonnegative"
    parsed = _model_query_plan(json.dumps(plan))
    assert parsed is not None
    with pytest.raises(DorisResultError) as caught:
        validate_reconciliation([{"conflicts": 17}], parsed["checks"])
    public = caught.value.as_public_dict()
    assert public["code"] == "inventory_quality_failed"
    assert "库存净额" in public["message"]
    assert public["details"] == {}
    assert "conflicts" not in json.dumps(public) and "17" not in json.dumps(public)
    restored = backend_error(502, {**public, "message": "PRIVATE SQL", "details": {"sql": "SELECT private"}})
    assert restored.as_public_dict() == public


@pytest.mark.parametrize("rule", ["private data", "SELECT secret", None, [], True, "unknown_rule"])
def test_validation_rule_cannot_carry_arbitrary_diagnostics(rule):
    plan = validation()
    plan["checks"][0]["rule"] = rule
    assert _model_query_plan(json.dumps(plan)) is None


def test_freshness_metadata_cannot_be_disguised_as_business_table():
    plan = business("SELECT update_time AS location, table_name AS label FROM information_schema.tables")
    assert _model_query_plan(json.dumps(plan)) is None


def test_metadata_can_supply_declared_freshness_validation_but_not_business_evidence(model_host):
    plan = validation()
    plan["sql"] = "SELECT CASE WHEN MAX(update_time) IS NULL THEN 1 ELSE 0 END AS conflicts FROM information_schema.tables"
    plan["checks"][0]["rule"] = "source_freshness"
    assert _model_query_plan(json.dumps(plan)) is not None
    thread, bridge = model_host
    thread.run.side_effect = [turn(plan), turn({"outcome": "completed", "answer": "已完成"})]
    bridge.query.return_value = payload([{"conflicts": 0}])
    result = execute()
    assert not result["query_execution"]["live_business_result_executed"]
    assert result["result_state"] == "failed"
    assert result["query_error"]["code"] == "query_failed"
    assert result["verified_business_result"] is None


@pytest.mark.parametrize("sql", [
    "SELECT * FROM ywzt_ods.master_wms",
    "SELECT COUNT(*) AS conflicts, SUM(1) AS conflicts FROM ywzt_ods.master_wms",
])
def test_validation_cannot_select_an_ambiguous_check_column(sql):
    assert _model_query_plan(json.dumps({**validation(), "sql": sql})) is None


@pytest.mark.parametrize("field,value", [
    ("validation_query_call_count", 2), ("validation_query_call_count", -1),
    ("validation_query_call_count", True), ("validation_query_call_count", 0.5),
    ("validation_query_call_count", "NaN"), ("validation_query_call_count", None),
    ("metadata_query_call_count", None), ("successful_business_query_call_count", 2),
])
def test_invalid_classification_counts_cannot_bypass_the_chat_evidence_gate(field, value):
    from app.codex_agent import query_execution_evidence
    from app.chat_api import _query_execution_evidence
    calls = [{"server": "doris_query_agent", "tool": "query_doris_sql", "status": "completed"}] * 2
    evidence = query_execution_evidence(calls, validation_query_call_count=1)
    evidence[field] = value
    normalized = _query_execution_evidence(calls, evidence)
    assert normalized["status"] == "evidence_invalid"
    assert not normalized["live_business_result_executed"]


def test_model_result_is_accepted_through_the_real_nonce_bound_runner(model_host, tmp_path, monkeypatch):
    import subprocess
    from app.chat_api import ChatRunner

    thread, bridge = model_host
    thread.run.side_effect = [turn(business()), turn(validation()), turn({"outcome": "completed", "answer": "已核对仓库资料。"})]
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]), payload([{"conflicts": 0}])]
    result = execute()
    runner = ChatRunner(project_root=tmp_path)
    monkeypatch.setattr(ChatRunner, "auth_configured", property(lambda _: True))

    def child(command, **kwargs):
        envelope = {"nonce": kwargs["env"]["AGENT_CHAT_INTERNAL_RESULT_NONCE"], "result": result}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    received = runner.run("查询仓库资料")
    assert received["query_execution"]["status"] == "live_result"
    assert received["query_execution"]["validation_query_call_count"] == 1
    assert received["verified_business_result"] == result["verified_business_result"]


@pytest.mark.parametrize("count", [2, 501])
def test_declarations_cross_chat_boundary_and_pagination_without_private_keys(tmp_path, monkeypatch, count):
    from fastapi.testclient import TestClient
    from app.chat_api import create_chat_app, IdentityResolver
    from app.chat_sessions import ChatSessionStore
    from app.tests.test_chat_api import FakeChatRunner

    runner = FakeChatRunner()
    model_result = {"status": "completed", "thread_id": "thread-projection-test",
        "result_state": "live_success", "interaction": {"outcome": "completed", "answer": "已按要求查询仓库资料。", "questions": []},
        "final_response": '{"outcome":"completed","answer":"已按要求查询仓库资料。"}',
        "mcp_calls": [{"server": "doris_query_agent", "tool": "query_doris_sql", "status": "completed"}],
        "query_execution": {"live_business_result_executed": True, "status": "live_result"},
        "verified_business_result": {"columns": ["location", "label"],
            "result_columns": business()["result_columns"],
            "rows": [{"location": f"{i:05d}", "label": "测试仓"} for i in range(count)]}}
    monkeypatch.setattr(runner, "run", lambda *a, **k: model_result)
    app = create_chat_app(runner, session_store=ChatSessionStore(tmp_path / "chat.sqlite3"),
                          identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        response = client.post("/v1/chat", json={"question": "查询仓库资料", "conversation_id": session["conversation_id"]},
                               headers={"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "projection-e2e-test"})
        assert response.status_code == 200
        table = response.json()["business_table"]
        assert [column["label"] for column in table["columns"]] == ["仓库编码", "仓库名称"]
        assert table["rows"][0] == {"col_1": "00000", "col_2": "测试仓"}
        assert "result_columns" not in response.text and '"location"' not in response.text
        if count > 500:
            page = table["pagination"]
            fetched = client.get(f"/v1/results/{page['result_id']}/pages/2")
            assert fetched.status_code == 200
            assert [column["label"] for column in fetched.json()["business_table"]["columns"]] == ["仓库编码", "仓库名称"]


def test_native_tool_evidence_uses_latest_business_answer_state():
    from types import SimpleNamespace
    from app.codex_agent import query_execution_evidence_from_items

    def item(rows):
        return SimpleNamespace(type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
            status="completed", arguments={"sql": "SELECT wms_code FROM ywzt_ods.master_wms"},
            result=SimpleNamespace(structured_content=payload(rows)), error=None)

    evidence = query_execution_evidence_from_items([item([{"wms_code": "synthetic"}]), item([])])
    assert evidence["successful_business_query_call_count"] == 2
    assert evidence["result_state"] == "no_data"
