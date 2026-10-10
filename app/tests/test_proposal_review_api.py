from copy import deepcopy
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.tests.test_knowledge_proposals import baseline, proposal


@pytest.fixture
def api(tmp_path, monkeypatch):
    runner = Mock(backend_url="http://127.0.0.1:9")
    runner.run.side_effect = AssertionError("proposal review must not invoke the model")
    semantic = Mock()
    semantic.run_if_supported.side_effect = AssertionError("proposal review must not query")
    health = Mock(side_effect=AssertionError("proposal review must not need Doris health"))
    monkeypatch.setattr("app.chat_api._read_backend_health", health)
    old = baseline()
    loader = Mock(side_effect=lambda: deepcopy(old))
    sessions = ChatSessionStore(tmp_path / "sessions.sqlite3")
    app = create_chat_app(runner, session_store=sessions, semantic_runtime=semantic,
                          proposal_baseline_loader=loader, identity_resolver=IdentityResolver(mode="anonymous"),
                          enforce_loopback=False)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        binding = {"conversation_id": conversation, "display_session_id": "ui-session-123", "request_id": "request-12345678"}
        yield app, client, headers, binding, old, loader
        runner.run.assert_not_called()
        semantic.run_if_supported.assert_not_called()
        health.assert_not_called()


def open_proposal(api, **fields):
    app, client, headers, binding, old, loader = api
    body = {**binding, "proposal": json.dumps(proposal(old, **(fields or {"formula": "stock - sales - transfer"}))) }
    response = client.post("/v1/knowledge-proposals", json=body, headers=headers)
    return response, body


def selection_body(opened, binding, choice="proposed"):
    return {**binding, "revision": opened["revision"],
            "choices": {q["id"]: choice for q in opened["assessment"]["questions"]}}


def test_safe_field_comparison_selection_recovery_and_exact_replay(api):
    app, client, headers, binding, old, loader = api
    response, body = open_proposal(api)
    assert response.status_code == 200
    opened = response.json()
    assert opened["result_state"] == "definition_review"
    assert opened["process"] == {"state": "not_queried", "steps": []}
    assessment = opened["assessment"]
    assert assessment["comparisons"][0]["before"] == "stock - sales"
    assert assessment["comparisons"][0]["after"] == "stock - sales - transfer"
    assert assessment["questions"][0]["prompt"] == "Available inventory：计算口径"
    assert assessment["questions"][0]["options"] == [
        {"id": "current", "label": "保留当前定义"}, {"id": "proposed", "label": "采用本次提案"},
        {"id": "defer", "label": "暂不确认"}]
    encoded = json.dumps(opened)
    assert all(forbidden not in encoded for forbidden in ["warehouse.stock", "inventory.available", "baseline_sha256", "before_sha256", "source_tables", "business_result", "business_table"])
    assert client.post("/v1/knowledge-proposals", json=body, headers=headers).json() == opened
    url = "/v1/knowledge-proposals/" + opened["proposal_id"]
    assert client.post(url + "/resume", json=binding, headers=headers).json() == opened
    selected_body = selection_body(opened, binding)
    selected = client.post(url + "/select", json=selected_body, headers=headers).json()
    assert selected["revision"] == 1
    assert selected["assessment"]["state"] == "proposal_selected_not_executed"
    assert selected["assessment"]["publication_authorized"] is False
    assert selected["assessment"]["comparisons"] == assessment["comparisons"]
    assert client.post(url + "/select", json=selected_body, headers=headers).json() == selected
    assert client.post(url + "/select", json=selection_body(opened, binding, "current"), headers=headers).status_code == 409
    assert client.post(url + "/resume", json=binding, headers=headers).json() == selected
    assert client.get("/v1/session").json()["conversation_id"] == binding["conversation_id"]
    assert old.contracts["metric:inventory.available"]["formula"] == "stock - sales"


def test_deferred_choices_resume_without_hiding_original_comparison(api):
    app, client, headers, binding, old, loader = api
    response, _ = open_proposal(api)
    opened = response.json()
    url = "/v1/knowledge-proposals/" + opened["proposal_id"] + "/select"
    deferred = client.post(url, json=selection_body(opened, binding, "defer"), headers=headers).json()
    assert deferred["assessment"]["requires_confirmation"] is True
    assert deferred["assessment"]["comparisons"] == opened["assessment"]["comparisons"]
    selected = client.post(url, json=selection_body(deferred, binding, "current"), headers=headers).json()
    assert selected["revision"] == 2
    assert selected["assessment"]["requires_confirmation"] is False


@pytest.mark.parametrize("operation", ["resume", "select", "cancel"])
@pytest.mark.parametrize("field", ["display_session_id", "request_id", "conversation_id"])
def test_cross_context_operations_are_rejected(api, field, operation):
    app, client, headers, binding, old, loader = api
    response, _ = open_proposal(api)
    opened = response.json()
    body = selection_body(opened, binding) if operation == "select" else dict(binding)
    body[field] = "other-context123"
    before = loader.call_count
    rejected = client.post("/v1/knowledge-proposals/" + opened["proposal_id"] + "/" + operation, json=body, headers=headers)
    assert rejected.status_code == 404
    assert loader.call_count == before


def test_csrf_origin_and_foreign_machine_cannot_create_or_select(api):
    app, client, headers, binding, old, loader = api
    response, body = open_proposal(api)
    opened = response.json()
    before = loader.call_count
    assert client.post("/v1/knowledge-proposals", json=body).status_code == 403
    assert client.post("/v1/knowledge-proposals", json=body, headers={**headers, "Origin": "http://foreign.invalid"}).status_code == 403
    with TestClient(app) as stranger:
        session = stranger.get("/v1/session").json()
        foreign_headers = {"X-CSRF-Token": session["csrf_token"]}
        assert stranger.post("/v1/knowledge-proposals", json=body, headers=foreign_headers).status_code == 404
        assert stranger.post("/v1/knowledge-proposals/" + opened["proposal_id"] + "/select", json=selection_body(opened, binding), headers=foreign_headers).status_code == 404
    assert loader.call_count == before


@pytest.mark.parametrize("bad", [None, {}, "{}", '{"x":1,"x":2}', " "*200001],
                         ids=["null", "object", "missing_fields", "duplicate", "oversized"])
def test_invalid_material_never_creates_a_pending_context(api, bad):
    app, client, headers, binding, old, loader = api
    response = client.post("/v1/knowledge-proposals", json={**binding, "proposal": bad}, headers=headers)
    assert response.status_code == 422
    assert not app.state.proposal_contexts._entries


@pytest.mark.parametrize("value", ["SELECT amount FROM private.orders", "SUM(ship_qty)",
    "sk-PRIVATECREDENTIALMARKER", "password=PRIVATECREDENTIALMARKER", "<script>PRIVATECREDENTIALMARKER</script>"])
def test_internal_or_unsafe_definitions_do_not_become_blind_choices_or_files(api, tmp_path, value):
    app, client, headers, binding, old, loader = api
    response, _ = open_proposal(api, formula=value)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "proposal_description_required"
    assert "PRIVATECREDENTIALMARKER" not in response.text
    assert not app.state.proposal_contexts._entries
    for path in tmp_path.rglob("*"):
        if path.is_file(): assert b"PRIVATECREDENTIALMARKER" not in path.read_bytes()


def test_structured_changes_require_business_description_instead_of_truncated_questions(api):
    app, client, headers, binding, old, loader = api
    response, _ = open_proposal(api, formula="stock - sales - transfer", filters={"threshold": 5})
    assert response.status_code == 422
    assert not app.state.proposal_contexts._entries


def test_baseline_drift_blocks_recovery_and_selection(api):
    app, client, headers, binding, old, loader = api
    response, body = open_proposal(api)
    opened = response.json()
    old.contracts["metric:inventory.available"]["unit"] = "boxes"
    url = "/v1/knowledge-proposals/" + opened["proposal_id"]
    for suffix, payload in [("resume", binding), ("select", selection_body(opened, binding))]:
        changed = client.post(url + "/" + suffix, json=payload, headers=headers)
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "proposal_baseline_changed"


def test_cancel_and_close_make_late_create_unavailable(api):
    app, client, headers, binding, old, loader = api
    response, body = open_proposal(api)
    opened = response.json()
    url = "/v1/knowledge-proposals/" + opened["proposal_id"]
    assert client.post(url + "/cancel", json=binding, headers=headers).json() == {"state": "cancelled"}
    assert client.post(url + "/resume", json=binding, headers=headers).status_code == 410
    assert client.post("/v1/knowledge-proposals", json=body, headers=headers).status_code == 410
    close = {key: value for key, value in binding.items() if key != "request_id"}
    assert client.post("/v1/knowledge-proposal-sessions/close", json=close, headers=headers).json() == {"state": "closed"}
    assert client.post("/v1/knowledge-proposals", json={**body, "request_id": "late-request123"}, headers=headers).status_code == 410
    assert client.post("/v1/knowledge-proposals", json={**body, "display_session_id": "new-session123"}, headers=headers).status_code == 200


def test_restart_does_not_recover_private_material_from_disk(api):
    app, client, headers, binding, old, loader = api
    response, body = open_proposal(api)
    opened = response.json()
    app.state.proposal_contexts._entries.clear()
    assert client.post("/v1/knowledge-proposals/" + opened["proposal_id"] + "/resume", json=binding, headers=headers).status_code == 404


def test_missing_trusted_baseline_is_a_service_error_not_a_live_result(api):
    app, client, headers, binding, old, loader = api
    loader.side_effect = ValueError("PRIVATE_BASELINE_MARKER")
    response, _ = open_proposal(api)
    assert response.status_code == 503
    assert "PRIVATE_BASELINE_MARKER" not in response.text
    assert not app.state.proposal_contexts._entries
