import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.adhoc_sql import AdHocSqlValidator
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.sql_conversion import conversion_guidance, convert_request, is_conversion_request


QUESTION = "把 MySQL 查询转换成 Doris，保留别名和业务口径"
INPUT = json.dumps({"sql": "SELECT id FROM legacy.headers WHERE id = :value",
                    "bindings": {"value": "PRIVATE_CONVERSION_MARKER"},
                    "table_mapping": {"legacy.headers": "ywzt_ods.headers"}})
SOURCES = [{"id": "ywzt_ods.headers", "fields": {"id": {"type": "string"}}}]


def convert(raw):
    from app.sql_conversion import decode_request
    return convert_request(decode_request(raw), sources=SOURCES,
                           validator=AdHocSqlValidator(allowed_schemas=("ywzt_ods",)))


@pytest.mark.parametrize("question", [QUESTION, "请将MySQL改写为Doris", "MySQL迁移到Doris，只转换不执行"])
def test_conversion_intake_is_recognized(question):
    assert is_conversion_request(question)


@pytest.mark.parametrize("question", ["昨天出库量", "查询Doris库存", "MySQL是什么", "说明转换失败的原因"])
def test_unrelated_questions_are_not_conversion(question):
    assert not is_conversion_request(question)


def test_missing_materials_are_collected_once_without_fabricating_a_conversion():
    service = Mock(side_effect=AssertionError("No input must not invoke translation"))
    result = conversion_guidance(None, converter=service)
    assert result["state"] == "needs_input"
    assert all(word in result["text"] for word in ("待转换", "对应关系", "参数"))
    service.assert_not_called()


def test_conversion_guidance_never_contains_query_or_bound_values():
    result = conversion_guidance(INPUT, converter=convert)
    assert result["state"] == "converted" and "未查询数据" in result["text"]
    assert "PRIVATE_CONVERSION_MARKER" not in json.dumps(result)
    assert not any(key in result for key in ("sql", "bindings", "rows", "source_tables"))


def test_rejection_is_not_conversion_success():
    result = conversion_guidance(INPUT.replace("SELECT id", "SELECT unknown"), converter=convert)
    assert result["state"] == "rejected"
    assert "未查询数据" in result["text"]


@pytest.mark.parametrize("queued", [False, True])
def test_chat_converts_once_without_model_database_or_private_persistence(tmp_path, monkeypatch, queued):
    runner = Mock(backend_url="http://127.0.0.1:9")
    runner.run.side_effect = AssertionError("conversion must not run the model")
    semantic = Mock()
    semantic.run_if_supported.side_effect = AssertionError("conversion must not query")
    health = Mock(side_effect=AssertionError("conversion does not need database health"))
    monkeypatch.setattr("app.chat_api._read_backend_health", health)
    translator = Mock(side_effect=convert)
    store = ChatSessionStore(tmp_path / "sessions.sqlite3")
    app = create_chat_app(runner, session_store=store, semantic_runtime=semantic,
                          sql_converter=translator, identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        bootstrap = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": bootstrap["csrf_token"], "Idempotency-Key": "req_conversion_12345678"}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        body = {"question": QUESTION, "conversation_id": conversation, "sql_conversion": INPUT}
        endpoint = "/v1/tasks" if queued else "/v1/chat"
        first = client.post(endpoint, headers=headers, json=body)
        assert first.status_code in (200, 202)
        result = first.json()
        if queued:
            import time
            for _ in range(100):
                result = client.get("/v1/tasks/" + first.json()["task_id"]).json()
                if result["state"] == "completed":
                    break
                time.sleep(0.01)
            result = result["result"]
        assert result["result_state"] == "explanation"
        assert result["guidance"]["state"] == "converted"
        assert result["outcome"]["label"] == "转换检查完成，未查询数据"
        assert result["process"] == {"state": "not_queried", "steps": []}
        assert result["clarification"] == {"required": False, "questions": []}
        assert "PRIVATE_CONVERSION_MARKER" not in json.dumps(result)
        assert not any(key in result for key in ("business_table", "business_result", "sql", "mcp_calls"))
        replay = client.post(endpoint, headers=headers, json=body)
        assert replay.status_code in (200, 202)
        assert translator.call_count == 1
        assert client.get("/v1/session").json()["conversation_id"] == conversation
        altered = {**body, "sql_conversion": INPUT.replace("PRIVATE_CONVERSION_MARKER", "OTHER")}
        assert client.post(endpoint, headers=headers, json=altered).status_code == 409
        assert client.post(endpoint, json=body).status_code == 403
        with TestClient(app) as stranger:
            other = stranger.get("/v1/session").json()
            assert stranger.post(endpoint, headers={"X-CSRF-Token": other["csrf_token"]}, json=body).status_code == 404
        runner.run.assert_not_called()
        semantic.run_if_supported.assert_not_called()
        health.assert_not_called()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"PRIVATE_CONVERSION_MARKER" not in path.read_bytes()


def test_mixed_query_and_conversion_input_is_rejected(tmp_path):
    app = create_chat_app(Mock(backend_url="http://127.0.0.1:9"),
                          session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
                          sql_converter=Mock(side_effect=AssertionError("mixed request")),
                          identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        headers = {"X-CSRF-Token": client.get("/v1/session").json()["csrf_token"]}
        for body in [
            {"question": "昨天库存", "sql_conversion": INPUT},
            {"question": QUESTION, "sql_conversion": {}},
            {"question": QUESTION, "sql_conversion": INPUT, "clarification_answers": []},
        ]:
            assert client.post("/v1/chat", headers=headers, json=body).status_code == 422


@pytest.mark.parametrize("raw,state,label", [
    (None, "needs_input", "待补充转换材料，未查询数据"),
    ("{}", "rejected", "转换未通过，未查询数据"),
])
def test_api_labels_match_conversion_state(tmp_path, raw, state, label):
    translator = Mock(side_effect=convert)
    app = create_chat_app(Mock(backend_url="http://127.0.0.1:9"),
                          session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
                          sql_converter=translator,
                          identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        headers = {"X-CSRF-Token": client.get("/v1/session").json()["csrf_token"]}
        body = {"question": QUESTION}
        if raw is not None:
            body["sql_conversion"] = raw
        response = client.post("/v1/chat", headers=headers, json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["guidance"]["state"] == state
        assert result["outcome"]["label"] == label
        assert result["process"] == {"state": "not_queried", "steps": []}
        assert translator.call_count == (0 if raw is None else 1)
