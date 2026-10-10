"""Explanatory requests must not fabricate a query or depend on Doris availability."""

import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.chat_api import create_chat_app, IdentityResolver, _public_business_result
from app.chat_sessions import ChatSessionStore
from app.request_guidance import large_result_guidance, query_outcome_guidance
from app.request_intent import assess_request


QUESTION = "Agent 查询返回异常时，请分别给出以下三种情况的回复模板：后端安全门禁拒绝、SQL 执行失败、SQL 成功但无业务数据。"


def test_status_templates_are_explanations_not_live_results():
    assert assess_request(QUESTION).kind == "explanation"
    guidance = query_outcome_guidance(QUESTION)
    assert guidance["kind"] == "query_outcomes"
    assert guidance["format"] == "plain_text"
    assert all(label in guidance["text"] for label in ["后端拒绝", "执行失败", "成功无数据", "结果截断"])
    assert "完整结果" in guidance["text"] and "分段" in guidance["text"]
    assert not any(term in guidance["text"].lower() for term in ["select ", "password", "secret", "token", "sql", "审计内容"])
    assert _public_business_result({"outcome": "completed", "answer": guidance["text"]}, live=False) is None


@pytest.mark.parametrize("question", [
    "请说明后端拒绝、执行失败和无数据有什么区别", "无数据和查询失败是什么意思？",
    "给出结果截断时的回复示例", "解释查询状态：拒绝、失败、无数据", QUESTION,
])
def test_explanation_paraphrases_do_not_need_a_metric_or_window(question):
    assert query_outcome_guidance(question) is not None


@pytest.mark.parametrize("question", [
    "昨天各仓库实际销售出库", "查询名称包含沈阳的仓库", "查询库存并给出无数据回复模板",
    "解释库存周转公式", "查昨天的出库，失败时给出回复模板", "查询今天SO07订单，失败时给出回复模板",
    "统计上月订单并说明失败和无数据的区别", "数据库失败了，重试查询库存", "昨天销售出库失败了",
])
def test_guidance_does_not_swallow_business_queries_or_other_explanations(question):
    assert query_outcome_guidance(question) is None


def test_large_result_plan_is_a_non_query_explanation():
    question = "查询近一年所有出库明细，后端返回结果达到行数上限。请说明如何处理，并给出按月、按仓库或按SKU拆分查询的方案。"
    guidance = large_result_guidance(question)
    assert guidance is not None
    assert guidance["kind"] == "large_result_plan"
    assert all(term in guidance["text"] for term in ("按月", "按仓库", "按 SKU", "左闭右开", "对账"))
    assert not any(term in guidance["text"].lower() for term in ("select ", "sql", "ywzt_ods", "审计"))


@pytest.mark.parametrize("question,guide", [
    (QUESTION, query_outcome_guidance),
    ("查询近一年所有出库明细，后端返回结果达到行数上限。请说明如何处理，并给出按月、按仓库或按SKU拆分查询的方案。", large_result_guidance),
])
def test_chat_explanation_is_screened_persisted_and_does_not_run_model_or_query(tmp_path, monkeypatch, question, guide):
    runner = Mock()
    runner.backend_url = "http://127.0.0.1:9"
    runner.run.side_effect = AssertionError("A status explanation must not invoke a model")
    semantic = Mock()
    semantic.run_if_supported.side_effect = AssertionError("A status explanation must not query Doris")
    health = Mock(side_effect=AssertionError("No database availability precondition for an explanation"))
    monkeypatch.setattr("app.chat_api._read_backend_health", health)
    store = ChatSessionStore(tmp_path / "sessions.sqlite3")
    with TestClient(create_chat_app(runner, session_store=store, semantic_runtime=semantic,
                                  identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)) as client:
        bootstrap = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": bootstrap["csrf_token"], "Idempotency-Key": "req_explanation_123456"}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        body = {"question": question, "conversation_id": conversation}
        first = client.post("/v1/chat", headers=headers, json=body)
        assert first.status_code == 200
        response = first.json()
        assert response["status"] == "completed" and response["result_state"] == "explanation"
        assert response["process"] == {"state": "not_queried", "steps": []}
        assert response["guidance"] == guide(question)
        assert response["clarification"] == {"required": False, "questions": []}
        assert not any(key in response for key in ("business_result", "business_table", "mcp_calls", "query_execution"))
        assert client.post("/v1/chat", headers=headers, json=body).json() == response
        assert client.get("/v1/session").json()["conversation_id"] == conversation
        assert client.post("/v1/chat", json=body).status_code == 403
        runner.run.assert_not_called()
        semantic.run_if_supported.assert_not_called()
        health.assert_not_called()
