"""T35: the browser receives a safe state, not private failure diagnostics."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.codex_agent import _last_verified_business_from_items
from app.query_outcomes import DorisResultError
from app.tests.test_chat_api import FakeChatRunner


@pytest.mark.parametrize("reason,http_status,state", [
    ("backend_rejected", 409, "rejected"),
    ("query_failed", 502, "failed"),
    ("result_truncated", 409, "truncated"),
])
def test_model_failure_keeps_request_context_isolated_and_returns_safe_error(tmp_path, reason, http_status, state):
    class Runner(FakeChatRunner):
        def run(self, question, **kwargs):
            return {
                "status": "completed", "thread_id": "offline-persistent-thread",
                "final_response": json.dumps({"outcome": "failed", "questions": []}),
                "query_error": {**DorisResultError(reason).as_public_dict(), "message": "PRIVATE_DIAGNOSTIC"},
                "query_execution": {"live_business_result_executed": False},
            }
    store = ChatSessionStore(tmp_path / "sessions.sqlite3")
    with TestClient(create_chat_app(Runner(), session_store=store, identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        headers["Idempotency-Key"] = "test_error_state_123"
        response = client.post("/v1/chat", headers=headers, json={"question": "查询测试仓库资料", "conversation_id": conversation})
        assert response.status_code == http_status
        assert response.json()["error"]["result_state"] == state
        expected_normalized = {
            "rejected": "query_rejected",
            "failed": "query_failed",
            "truncated": "truncated_result",
        }[state]
        assert response.json()["error"]["normalized_result_state"] == expected_normalized
        assert "PRIVATE_DIAGNOSTIC" not in response.text
        assert "clarification" not in response.json() and "business_result" not in response.json()
        replay = client.post("/v1/chat", headers=headers, json={"question": "查询测试仓库资料", "conversation_id": conversation})
        assert replay.json() == response.json()
        context = client.get("/v1/session").json()["context_state"]
        assert not context["has_context"]
        assert context["query_context_mode"] == "fresh_per_request"
        assert context["learning_summary_only"] is True


def test_successful_empty_response_is_not_labeled_as_failed(tmp_path):
    class Runner(FakeChatRunner):
        def run(self, question, **kwargs):
            return {
                "status": "completed", "thread_id": "offline-empty-thread", "result_state": "no_data",
                "final_response": json.dumps({"outcome": "completed", "answer": "查询成功，当前范围没有匹配的仓库资料"}),
                "query_execution": {"live_business_result_executed": True, "live_query_executed": True, "status": "live_result"},
            }
    with TestClient(create_chat_app(Runner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"), identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        response = client.post("/v1/chat", headers=headers, json={"question": "查询测试仓库资料", "conversation_id": conversation})
        assert response.status_code == 200
        assert response.json()["result_state"] == "no_data"
        assert response.json()["status"] == "completed"
        assert not response.json()["clarification"]["required"]


def test_inventory_data_failure_preserves_safe_cause_and_cannot_leak_rows(tmp_path):
    class Runner(FakeChatRunner):
        def run(self, question, **kwargs):
            return {
                "status": "completed", "thread_id": "offline-inventory-thread",
                "final_response": json.dumps({"outcome": "failed", "questions": []}),
                "query_error": {**DorisResultError("reconciliation_failed", validation_rule="inventory_net_nonnegative").as_public_dict(),
                                "message": "PRIVATE_SQL", "details": {"row": "PRIVATE_ROW"}},
                "query_execution": {"live_business_result_executed": False},
            }
    with TestClient(create_chat_app(Runner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
                                    identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        response = client.post("/v1/chat", headers=headers,
                               json={"question": "查询测试库存", "conversation_id": conversation})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "inventory_quality_failed"
        assert "库存净额" in response.json()["error"]["message"]
        assert "PRIVATE" not in response.text and "business_table" not in response.text


def test_live_prose_without_a_verified_table_is_not_completed(tmp_path):
    class Runner(FakeChatRunner):
        def run(self, question, **kwargs):
            return {
                "status": "completed", "thread_id": "offline-prose-thread",
                "result_state": "live_success",
                "final_response": json.dumps({"outcome": "completed", "answer": "库存为128箱"}),
                "query_execution": {"live_business_result_executed": True, "status": "live_result"},
            }

    with TestClient(create_chat_app(Runner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"), identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "missing-table-regression"}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        request = {"question": "查询测试库存", "conversation_id": conversation}
        response = client.post("/v1/chat", headers=headers, json=request)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "query_invalid_result"
        assert "结果校验" in response.json()["error"]["message"]
        assert response.json()["error"]["details"] == {}
        assert "business_result" not in response.json()
        assert "128" not in response.text
        assert client.post("/v1/chat", headers=headers, json=request).json() == response.json()


def test_gateway_rows_without_columns_are_rejected(tmp_path):
    class Runner(FakeChatRunner):
        def run(self, question, **kwargs):
            item = SimpleNamespace(
                type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
                status="completed", arguments={"sql": "SELECT quantity FROM ywzt_ods.example"},
                result={"structuredContent": {
                    "status": "succeeded", "execution": {
                        "executor_name": "doris", "is_mock": False,
                        "truncated": False, "row_count": 1, "rows": [{"quantity": "128"}],
                    },
                }},
            )
            return {
                "status": "completed", "thread_id": "offline-projection-thread",
                "result_state": "live_success",
                "final_response": json.dumps({"outcome": "completed", "answer": "库存999999箱"}),
                "query_execution": {"live_business_result_executed": True, "status": "live_result"},
                "verified_business_result": _last_verified_business_from_items([item]),
            }

    with TestClient(create_chat_app(Runner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"), identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        response = client.post("/v1/chat", headers=headers, json={"question": "查询测试库存", "conversation_id": conversation})
        assert response.status_code == 502
        body = response.json()
        assert body["error"]["code"] in {"chat_execution_error", "query_invalid_result"}
        assert "business_table" not in body
        assert "999999" not in response.text
        assert "SELECT" not in response.text and "ywzt_ods" not in response.text


def test_invalid_result_exposes_only_safe_missing_fields(tmp_path):
    class Runner(FakeChatRunner):
        def run(self, question, **kwargs):
            return {
                "status": "completed", "thread_id": "offline-invalid-thread",
                "final_response": json.dumps({"outcome": "failed", "questions": []}),
                "query_error": DorisResultError(
                    "execution_invalid",
                    missing_fields=("execution.rows", "execution.columns"),
                ).as_public_dict(),
                "query_execution": {"live_business_result_executed": False},
            }

    with TestClient(create_chat_app(Runner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
                                    identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        response = client.post("/v1/chat", headers=headers,
                               json={"question": "查询测试库存", "conversation_id": conversation})
        body = response.json()
        assert response.status_code == 502
        assert body["error"]["normalized_result_state"] == "invalid_result"
        assert body["error"]["details"] == {"missing_fields": ["execution.rows", "execution.columns"]}
        assert "execution.rows" in body["error"]["message"]
