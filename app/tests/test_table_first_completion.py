"""Result size must not turn reconciled tables into business clarifications."""

import pytest
from fastapi.testclient import TestClient

from app.agent_core.semantic_types import SemanticResult
from app.chat_api import IdentityResolver, _safe_interaction, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_semantic_runtime import RecordingQueryClient, runtime


@pytest.mark.parametrize("days,warehouse_count", [(7, 156), (14, 157), (30, 200)])
def test_many_warehouse_rows_complete_through_the_chat_api(tmp_path, days, warehouse_count):
    rows = [
        {"wms_code": f"W{index:03d}", "warehouse_name": f"测试仓{index:03d}",
         "outbound_ea": 1, "total_ea": warehouse_count, "warehouse_count": warehouse_count}
        for index in range(warehouse_count)
    ]
    service = runtime(RecordingQueryClient({"actual sales outbound by warehouse": rows}))
    with TestClient(create_chat_app(
        FakeChatRunner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        semantic_runtime=service, identity_resolver=IdentityResolver(mode="anonymous"),
        enforce_loopback=False,
    )) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        response = client.post("/v1/chat", headers=headers, json={
            "conversation_id": conversation,
            "question": f"近{days}个完整自然日各仓库实际销售出库量，用表格展示。",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["result_state"] == "live_success"
        assert body["clarification"]["required"] is False
        assert len(body["business_table"]["rows"]) == warehouse_count
        assert sum(int(row["quantity"]) for row in body["business_table"]["rows"]) == warehouse_count
        assert body["business_result"]["text"]


@pytest.mark.parametrize("text", ["正常结果\n" * 81, "正常结果" * 201, ("正常结果" * 25 + "\n") * 80], ids=["line-count", "line-length", "total-length"])
def test_only_trusted_host_prose_can_be_replaced_with_a_table_summary(text):
    raw = {
        "interaction": {"outcome": "completed", "questions": [], "answer": text},
        "query_execution": {"live_business_result_executed": True},
    }
    assert _safe_interaction(raw)["outcome"] == "blocked"
    safe = _safe_interaction(SemanticResult(raw))
    assert safe == {"outcome": "completed", "questions": []}


@pytest.mark.parametrize("mutation", [
    {"answer": "正常结果\n" * 81 + "SELECT secret FROM private.table"},
    {"answer": "正常结果\n" * 81 + "该口径尚待业务审批"},
    {"questions": "invalid"},
    {"outcome": "failed"},
])
def test_table_summary_cannot_override_invalid_or_unapproved_interaction(mutation):
    result = SemanticResult({
        "interaction": {"outcome": "completed", "questions": [], "answer": "正常结果\n" * 81, **mutation},
        "query_execution": {"live_business_result_executed": True},
    })
    assert _safe_interaction(result)["outcome"] != "completed"


@pytest.mark.parametrize("outcome", ["failed", "blocked"])
def test_noncompleted_response_does_not_publish_partial_tables(tmp_path, outcome):
    class Runner(FakeChatRunner):
        def run(self, question):
            result = super().run(question)
            result["interaction"] = {"outcome": outcome, "questions": []}
            return result

    with TestClient(create_chat_app(
        Runner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False,
    )) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        body = client.post("/v1/chat", headers=headers, json={
            "question": "查询测试库存", "conversation_id": conversation,
        }).json()
        assert body["status"] == outcome
        assert not {"business_table", "business_tables", "business_result"}.intersection(body)
