"""Fixed diagnostic codes can aid planning without disclosing backend text."""

import asyncio
import json

import pytest
from mcp.client import Client

from app.codex_agent import _model_query_retry_prompt
from app.codex_mcp_bridge import _payload_from_result, _verify_live_payload, CodexMcpBridgeError
from app.errors import QueryExecutionError
from app.mcp_server import HttpQueryBackend, build_mcp_server
from app.query_diagnostics import classify_query_error, safe_diagnostic, safe_host_failure
from app.query_outcomes import backend_error


@pytest.mark.parametrize("code,message,expected", [
    (1054, "PRIVATE FIELD", "unknown_column"),
    (1146, "PRIVATE TABLE", "unknown_table"),
    (1052, "PRIVATE FIELD", "ambiguous_column"),
    (1064, "PRIVATE SQL", "syntax_error"),
    (1305, "PRIVATE SQL", "unsupported_function"),
    (1142, "PRIVATE USER", "permission_denied"),
    (2006, "PRIVATE HOST", "transport_error"),
    (1105, "No matching function for secret_fn(private_column)", "unsupported_function"),
    (1105, "Cannot cast private data to DECIMAL", "type_mismatch"),
    (1105, "Memory limit exceeded for private query", "resource_limit"),
    (1105, "Query timed out: private query", "query_timeout"),
    (1105, "unknown column private_column", "unknown_column"),
    (1105, "unrecognized private failure", None),
])
def test_driver_diagnostic_is_a_category_not_message(code, message, expected):
    error = QueryExecutionError("secret", details={"db_error_code": code, "message": message})
    public = error.as_public_dict()
    assert public["details"].get("diagnostic") == expected
    assert message not in json.dumps(public)
    assert "secret" not in json.dumps(public)


@pytest.mark.parametrize("value", [None, "SELECT private", "unknown_column\nsecret", {}, ["unknown_column"]])
def test_unrecognized_diagnostics_are_not_forwarded(value):
    assert safe_diagnostic(value) is None
    assert classify_query_error({"diagnostic": value}) is None
    prompt = _model_query_retry_prompt("current request", "query_failed", diagnostic=value)
    assert "SELECT private" not in prompt and "\nsecret" not in prompt


@pytest.mark.parametrize("diagnostic,reason", [("unknown_column", "query_failed"), ("permission_denied", "backend_rejected")])
def test_http_mcp_and_host_preserve_only_fixed_code(monkeypatch, diagnostic, reason):
    backend = HttpQueryBackend("http://127.0.0.1:8010")
    wire = json.dumps({"error": {"code": "query_execution_error", "message": "PRIVATE_TEXT",
        "details": {"diagnostic": diagnostic, "sql": "SELECT secret", "rows": ["PRIVATE_ROW"]}}}).encode()
    monkeypatch.setattr(backend, "_query_once", lambda _: (502, wire))
    monkeypatch.setattr("app.mcp_server.time.sleep", lambda _: None)

    async def check():
        async with Client(build_mcp_server(backend)) as client:
            result = await client.call_tool("query_doris_sql", {"sql": "SELECT 1"})
            payload = _payload_from_result(result)
            assert payload["error"]["diagnostic"] == diagnostic
            assert "PRIVATE" not in json.dumps(payload) and "secret" not in json.dumps(payload)
            with pytest.raises(CodexMcpBridgeError) as caught:
                _verify_live_payload(payload)
            assert caught.value.reason == reason
            assert caught.value.diagnostic == diagnostic
            public = caught.value.as_public_dict()
            assert public["details"] == {} and "diagnostic" not in public
    asyncio.run(check())


def test_arbitrary_backend_details_cannot_become_a_model_instruction():
    error = backend_error(502, {"code": "query_execution_error", "diagnostic": "SELECT secret",
                               "details": {"sql": "SELECT secret"}})
    assert error.diagnostic is None
    assert "secret" not in json.dumps(error.as_tool_dict())


@pytest.mark.parametrize("value", [None, [], "private", {"stage": "SELECT private", "diagnostic": "password=private", "step": True},
                                     {"stage": [], "step": -1}, {"stage": {}, "step": "private"}])
def test_host_failure_logs_only_fixed_categories(value):
    assert safe_host_failure(value) == {"stage": "unknown", "diagnostic": "unspecified", "step": 0}


def test_host_failure_retains_known_operational_metadata_only():
    assert safe_host_failure({"stage": "query", "diagnostic": "unknown_column", "step": 2,
                              "sql": "SELECT private", "rows": ["private"]}) == {
        "stage": "query", "diagnostic": "unknown_column", "step": 2}


def test_nonce_bound_runner_does_not_log_private_error_fields(tmp_path, monkeypatch, caplog):
    import subprocess
    from app.chat_api import ChatRunner

    runner = ChatRunner(project_root=tmp_path)
    monkeypatch.setattr(ChatRunner, "auth_configured", property(lambda _: True))
    result = {"query_error": {"code": "query_failed"}, "host_failure": {
        "stage": "query", "diagnostic": "unknown_column", "step": 2,
        "sql": "SELECT PRIVATE_TEXT", "rows": ["PRIVATE_ROW"]}}

    def child(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "nonce": kwargs["env"]["AGENT_CHAT_INTERNAL_RESULT_NONCE"], "result": result}), stderr="PRIVATE_STDERR")

    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    assert runner.run("查询仓库资料") == result
    assert "stage=query diagnostic=unknown_column step=2" in caplog.text
    assert "PRIVATE" not in caplog.text
