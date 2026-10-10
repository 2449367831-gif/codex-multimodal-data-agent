from copy import deepcopy
from datetime import datetime

from fastapi.testclient import TestClient
import pytest

from app.agent_core.outbound_links import OutboundLinkPlan
from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_outbound_link_route import QUESTION
from app.tests.test_outbound_links import links, execute


@pytest.mark.parametrize("mode", ["exact", "latest", "all"])
def test_chat_result_is_owned_idempotent_and_never_reasks_model(links, tmp_path, monkeypatch, mode):
    monkeypatch.setattr("app.agent_core.outbound_link_planning.active_link_contract", lambda _: True)
    plan = OutboundLinkPlan(outbound_codes=("OUT-1",) if mode == "exact" else (), latest=mode == "latest")
    rows = execute(links, plan)
    class Query:
        calls = 0
        def query(self, **kwargs):
            self.calls += 1
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
                "rows": deepcopy(rows), "row_count": len(rows), "truncated": False}}
    class Runner(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError("reviewed link query must not call the model")
    query = Query()
    runtime = SemanticQueryRuntime(query_client=query, clock=lambda: datetime(2026, 9, 11, 12))
    app = create_chat_app(Runner(), semantic_runtime=runtime, session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client, TestClient(app) as other:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "link-first-123456789"}
        question = "查询出库单号 OUT-1 的关联链路" if mode == "exact" else QUESTION
        body = {"question": question, "conversation_id": session["conversation_id"]}
        first = client.post("/v1/chat", headers=headers, json=body)
        assert first.status_code == 200, first.json()
        conversation_id = first.json()["conversation_id"]
        if mode != "exact":
            card = first.json()["clarification"]
            assert card["required"] and len(card["questions"]) == 1 and query.calls == 0
            body = {"question": "确认并继续", "conversation_id": conversation_id,
                "clarification_token": card["token"], "clarification_answers": [
                    {"question_id": "outbound_link_scope", "option_ids": [f"outbound_link_{mode}"]}]}
            headers = {**headers, "Idempotency-Key": "link-choice-123456789"}
            first = client.post("/v1/chat", headers=headers, json=body)
        result = first.json()
        assert first.status_code == 200 and result["result_state"] == "live_success", result
        assert not result["clarification"]["required"] and query.calls == 2
        table = result["business_table"]
        assert len(table["columns"]) == 11 and len(table["notes"]) == 3
        assert len(table["rows"]) == len(rows)
        replay = client.post("/v1/chat", headers=headers, json=body)
        assert replay.json()["business_table"] == table and query.calls == 2
        refreshed = client.get("/v1/session").json()
        assert refreshed["conversation_id"] == conversation_id
        foreign = other.get("/v1/session").json()
        response = other.post("/v1/chat", headers={"X-CSRF-Token": foreign["csrf_token"]}, json={**body, "conversation_id": conversation_id})
        assert response.status_code >= 400 and query.calls == 2


def test_backend_failure_is_not_link_no_data(links, tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent_core.outbound_link_planning.active_link_contract", lambda _: True)
    class Query:
        def query(self, **kwargs):
            raise TimeoutError("private detail must not escape")
    app = create_chat_app(FakeChatRunner(), semantic_runtime=SemanticQueryRuntime(query_client=Query()),
        session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"), identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        response = client.post("/v1/chat", headers={"X-CSRF-Token": session["csrf_token"]},
            json={"question": "查询出库单号 OUT-1 的关联链路", "conversation_id": session["conversation_id"]})
        assert response.status_code >= 400 and "private detail" not in response.text
        assert "business_table" not in response.json()
