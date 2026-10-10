"""T35/T36: evidence and error states are independent of business answers."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.client import Client

from app.query_outcomes import DorisResultError, backend_error, validate_doris_result
from app.mcp_server import HttpQueryBackend, build_mcp_server
from app.codex_mcp_bridge import _payload_from_result, _verify_live_payload, CodexMcpBridgeError
from app.agent_core.execution import QueryExecutionSession
from app.agent_core.semantic_types import SemanticRuntimeError
from app.query_scope import QueryScope


def test_reconciliation_invalid_result_keeps_public_failure_category():
    error = backend_error(502, {
        "code": "query_invalid_result",
        "failure_reason": "reconciliation_failed",
        "message": "untrusted message must not control error classification",
    })
    assert error.reason == "reconciliation_failed"
    assert error.as_public_dict()["message"] == "查询已执行，但对账校验未通过，未展示结果"


def payload(rows=None):
    rows = [] if rows is None else rows
    return {"status": "succeeded", "execution": {
        "executor_name": "doris", "is_mock": False, "truncated": False,
        "columns": list(rows[0]) if rows and isinstance(rows[0], dict) else [],
        "rows": rows, "row_count": len(rows),
    }}


@pytest.mark.parametrize("rows,state", [([], "no_data"), ([{"test": 0}], "live_success")])
def test_empty_success_is_not_a_failure_or_an_invented_zero(rows, state):
    assert validate_doris_result(payload(rows)).state == state


@pytest.mark.parametrize("field,value,reason,state", [
    ("truncated", True, "result_truncated", "truncated"),
    ("truncated", None, "execution_invalid", "failed"),
    ("is_mock", True, "not_live_result", "failed"),
    ("executor_name", "mock", "not_live_result", "failed"),
    ("row_count", True, "execution_invalid", "failed"),
    ("row_count", None, "execution_invalid", "failed"),
    ("rows", [None], "rows_invalid", "failed"),
])
def test_incomplete_and_unverifiable_evidence_has_specific_safe_state(field, value, reason, state):
    response = payload()
    response["execution"][field] = value
    with pytest.raises(DorisResultError) as caught:
        validate_doris_result(response)
    assert caught.value.reason == reason
    assert caught.value.result_state == state
    with pytest.raises(CodexMcpBridgeError) as bridge:
        _verify_live_payload(response)
    assert bridge.value.reason == reason


def test_missing_row_count_is_not_inferred_from_visible_rows():
    response = payload([{"test": 1}])
    response["execution"].pop("row_count")
    with pytest.raises(DorisResultError, match="校验"):
        validate_doris_result(response)


@pytest.mark.parametrize("http_status,code,reason,state", [
    (409, "sql_safety_error", "backend_rejected", "rejected"),
    (403, "forbidden", "backend_rejected", "rejected"),
    (502, "query_execution_error", "query_failed", "failed"),
    (503, "executor_unavailable", "backend_unavailable", "failed"),
    (404, "http_404", "backend_unavailable", "failed"),
])
def test_http_error_class_survives_without_echoing_backend_diagnostics(monkeypatch, http_status, code, reason, state):
    backend = HttpQueryBackend("http://127.0.0.1:8010")
    wire = json.dumps({"error": {"code": code, "message": "PRIVATE_DIAGNOSTIC", "details": {"sql": "SELECT secret"}}}).encode()
    monkeypatch.setattr(backend, "_query_once", lambda _: (http_status, wire))
    monkeypatch.setattr("app.mcp_server.time.sleep", lambda _: None)
    with pytest.raises(DorisResultError) as caught:
        backend.query(sql="SELECT 1")
    assert caught.value.reason == reason
    assert caught.value.result_state == state
    public = json.dumps(caught.value.as_public_dict())
    assert "PRIVATE_DIAGNOSTIC" not in str(caught.value) + public
    assert "SELECT" not in public and "secret" not in public


@pytest.mark.parametrize("details,diagnostic", [
    ({"db_error_code": 1054}, "unknown_column"),
    ({"db_error_code": 1105, "message": "Unknown column in analysis"}, "unknown_column"),
])
def test_nested_doris_diagnostic_is_preserved_for_private_model_repair(details, diagnostic):
    error = backend_error(502, {
        "code": "query_execution_error",
        "message": "Doris query execution failed",
        "details": details,
    })
    assert error.diagnostic == diagnostic
    assert "diagnostic" not in error.as_public_dict()
    assert error.as_tool_dict()["diagnostic"] == diagnostic


def test_mcp_keeps_structured_failure_without_turning_it_into_success():
    class Backend:
        def query(self, **_):
            raise DorisResultError("backend_rejected")

    async def check():
        async with Client(build_mcp_server(Backend())) as client:
            result = await client.call_tool("query_doris_sql", {"sql": "SELECT 1"})
            structured = _payload_from_result(result)
            assert structured["status"] == "failed"
            with pytest.raises(CodexMcpBridgeError) as caught:
                _verify_live_payload(structured)
            assert caught.value.result_state == "rejected"
    asyncio.run(check())


def test_deterministic_execution_preserves_rejection_as_public_code():
    class Backend:
        def query(self, **_):
            raise DorisResultError("backend_rejected")
    session = QueryExecutionSession(Backend(), QueryScope(), "HJXH")
    with pytest.raises(SemanticRuntimeError) as caught:
        session.query("SELECT 1", purpose="offline error fixture")
    assert caught.value.as_public_dict()["code"] == "query_rejected"
    assert caught.value.as_public_dict()["result_state"] == "rejected"
    assert session.evidence()["live_business_result_executed"] is False


def test_business_result_evidence_tracks_successful_empty_rows():
    class Backend:
        def query(self, **_):
            return payload()
    session = QueryExecutionSession(Backend(), QueryScope(), "HJXH")
    session.query("SELECT * FROM ywzt_ods.master_wms", purpose="offline empty fixture")
    assert session.evidence()["result_state"] == "no_data"


def test_error_content_from_mcp_is_not_echoed():
    with pytest.raises(CodexMcpBridgeError) as caught:
        _payload_from_result(SimpleNamespace(is_error=True, structured_content=None, content=["PRIVATE_DIAGNOSTIC"]))
    assert "PRIVATE_DIAGNOSTIC" not in str(caught.value)
