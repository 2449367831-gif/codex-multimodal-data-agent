from copy import deepcopy
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.codex_interpretation import InterpretationRunError
from app.proposal_interpretation import interpretation_input, validate_interpretation
from app.tests.test_knowledge_proposals import baseline
from app.tests.test_proposal_interpretation import QUESTION, draft

SIMPLE = "请将库存计算规则改成同时扣除销售和调拨占用。"


@pytest.fixture
def api(tmp_path, monkeypatch):
    old = baseline()
    query = Mock(backend_url="http://127.0.0.1:9")
    query.run.side_effect = AssertionError("not a data request")
    semantic = Mock()
    semantic.run_if_supported.side_effect = AssertionError("no Doris calls")
    health = Mock(side_effect=AssertionError("no health-as-business-result"))
    monkeypatch.setattr("app.chat_api._read_backend_health", health)
    state = SimpleNamespace(calls=[], started=Event(), hold=False, error=None)
    class Interpreter:
        project_root = tmp_path
        def run(self, question, snapshot, **kwargs):
            state.calls.append((question, deepcopy(snapshot.contracts), kwargs))
            thread_id = kwargs["thread_id"] or "thread-" + kwargs["context_home"].name[:16]
            kwargs["on_context_ready"](thread_id)
            state.started.set()
            while state.hold and not kwargs["cancel_event"].wait(0.01): pass
            if kwargs["cancel_event"].is_set(): raise InterpretationRunError("interpretation_cancelled")
            if state.error: raise InterpretationRunError(state.error)
            raw = draft(snapshot, question)
            confirmation = kwargs["confirmation"]
            if question == SIMPLE:
                raw["segments"], raw["questions"] = raw["segments"][:1], []
            elif confirmation:
                prepared = interpretation_input(snapshot, question, confirmation=confirmation)
                raw["request_sha256"] = prepared["request_sha256"]
                raw["questions"] = []
                raw["segments"][1].update(role="unchanged", question_id=None, targets=[{
                    "kind": "metric", "id": "inventory.available", "field": "unit",
                    "value_sha256": prepared["field_sha256"]["metric:inventory.available"]["unit"],
                }])
            result = validate_interpretation(snapshot, question, raw, confirmation=confirmation)
            return {"thread_id": thread_id, "query_executed": False, "interpretation": result}
    sessions = ChatSessionStore(tmp_path / "sessions.sqlite3")
    app = create_chat_app(query, session_store=sessions, semantic_runtime=semantic,
                          proposal_baseline_loader=lambda: deepcopy(old), interpretation_runner=Interpreter(),
                          identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        bootstrap = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": bootstrap["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        binding = {"conversation_id": conversation, "display_session_id": "display-123", "request_id": "request-123"}
        yield SimpleNamespace(app=app, client=client, headers=headers, binding=binding, old=old, state=state, sessions=sessions)
    query.run.assert_not_called()
    semantic.run_if_supported.assert_not_called()
    health.assert_not_called()


def open_request(api, question=SIMPLE, binding=None):
    response = api.client.post("/v1/knowledge-interpretations", headers=api.headers,
                               json={**(binding or api.binding), "question": question})
    assert response.status_code == 200, response.text
    return response.json()


def wait_result(api, opened, binding=None):
    deadline = time.monotonic() + 5
    while True:
        response = api.client.post("/v1/knowledge-interpretations/" + opened["interpretation_id"] + "/resume",
                                   headers=api.headers, json=binding or api.binding)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["state"] != "running": return result
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_natural_language_to_owned_review_then_select_without_execution(api):
    opened = open_request(api)
    completed = wait_result(api, opened)
    assert completed["state"] == "review_ready" and len(api.state.calls) == 1
    assert completed["process"] == {"state": "not_queried", "steps": []}
    assert set(completed["proposal"]) == {"proposal_id"}
    url = "/v1/knowledge-proposals/" + completed["proposal"]["proposal_id"]
    review = api.client.post(url + "/resume", headers=api.headers, json=api.binding).json()
    assert review["assessment"]["comparisons"][0]["after"] == "stock - sales - transfer"
    selection = {**api.binding, "revision": review["revision"],
                 "choices": {q["id"]: "proposed" for q in review["assessment"]["questions"]}}
    selected = api.client.post(url + "/select", headers=api.headers, json=selection).json()
    assert selected["assessment"]["publication_authorized"] is False
    assert api.old.contracts["metric:inventory.available"]["formula"] == "stock - sales"
    assert open_request(api)["interpretation_id"] == opened["interpretation_id"]
    assert len(api.state.calls) == 1
    assert wait_result(api, opened)["proposal"] == completed["proposal"]


def test_all_questions_one_submission_defer_and_exact_replay_resume_same_thread(api):
    opened = open_request(api, QUESTION)
    pending = wait_result(api, opened)
    assert pending["state"] == "needs_confirmation"
    assert sum(o["label"] == "暂不确认" for o in pending["questions"][0]["options"]) == 1
    url = "/v1/knowledge-interpretations/" + opened["interpretation_id"] + "/select"
    body = {**api.binding, "revision": pending["revision"], "choices": {}}
    assert api.client.post(url, headers=api.headers, json=body).status_code == 422
    body["choices"] = {q["id"]: "defer" for q in pending["questions"]}
    deferred = api.client.post(url, headers=api.headers, json=body).json()
    assert deferred["state"] == "deferred" and len(api.state.calls) == 1
    assert api.client.post(url, headers=api.headers, json=body).json() == deferred
    body.update(revision=deferred["revision"], choices={q["id"]: q["options"][0]["id"] for q in pending["questions"]})
    assert api.client.post(url, headers=api.headers, json=body).status_code == 200
    completed = wait_result(api, opened)
    assert completed["state"] == "review_ready" and len(api.state.calls) == 2
    assert api.client.post(url, headers=api.headers, json=body).json() == completed
    first, second = api.state.calls
    assert first[0] == second[0] == QUESTION
    assert first[2]["context_home"] == second[2]["context_home"]
    assert first[2]["thread_id"] is None and second[2]["thread_id"].startswith("thread-")
    assert second[2]["confirmation"][0]["selected_option"] == "ea"


@pytest.mark.parametrize("close", [False, True])
def test_cancel_or_delete_while_model_runs_discards_late_result_and_releases_lease(api, close):
    api.state.hold = True
    opened = open_request(api)
    assert api.state.started.wait(3)
    with api.sessions._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_context_leases_v1").fetchone()[0] == 1
    if close:
        response = api.client.post("/v1/knowledge-proposal-sessions/close", headers=api.headers,
                                   json={key: value for key, value in api.binding.items() if key != "request_id"})
    else:
        response = api.client.post("/v1/knowledge-interpretations/" + opened["interpretation_id"] + "/cancel",
                                   headers=api.headers, json=api.binding)
    assert response.status_code == 200
    assert wait_result(api, opened)["state"] == "cancelled"
    deadline = time.monotonic() + 3
    while True:
        with api.sessions._connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM chat_context_leases_v1").fetchone()[0]
        if not count: break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert not api.app.state.proposal_contexts._entries


@pytest.mark.parametrize("operation", ["resume", "select", "cancel"])
@pytest.mark.parametrize("field", ["conversation_id", "display_session_id", "request_id"])
def test_foreign_request_bindings_never_read_or_modify_interpretations(api, field, operation):
    opened = open_request(api)
    wait_result(api, opened)
    body = {**api.binding, field: "foreign-123"}
    if operation == "select": body.update(revision=0, choices={})
    response = api.client.post("/v1/knowledge-interpretations/" + opened["interpretation_id"] + "/" + operation,
                               headers=api.headers, json=body)
    assert response.status_code == 404
    assert len(api.state.calls) == 1


def test_missing_csrf_and_foreign_installation_cannot_invoke_model(api):
    body = {**api.binding, "question": SIMPLE}
    assert api.client.post("/v1/knowledge-interpretations", json=body).status_code == 403
    with TestClient(api.app) as foreign:
        bootstrap = foreign.get("/v1/session").json()
        response = foreign.post("/v1/knowledge-interpretations", json=body,
                                headers={"X-CSRF-Token": bootstrap["csrf_token"]})
        assert response.status_code == 404
    assert api.state.calls == []


def test_failure_retains_canonical_identity_but_no_turn_success_or_private_error(api):
    api.state.error = "PRIVATE_PROVIDER_DETAIL"
    opened = open_request(api)
    failed = wait_result(api, opened)
    assert failed["state"] == "failed" and "PRIVATE" not in str(failed)
    with api.sessions._connection() as connection:
        thread, turns = connection.execute("SELECT codex_thread_id,turn_count FROM chat_principal_contexts_v1").fetchone()
    assert thread.startswith("thread-") and turns == 0
    assert not api.app.state.proposal_contexts._entries


def test_changed_baseline_does_not_publish_a_late_candidate(api):
    api.state.hold = True
    opened = open_request(api)
    assert api.state.started.wait(3)
    api.old.contracts["metric:inventory.available"]["unit"] = "boxes"
    api.state.hold = False
    deadline = time.monotonic() + 3
    while True:
        entries = list(api.app.state.interpretation_contexts._entries.values())
        if entries[0].state == "failed": break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    response = api.client.post("/v1/knowledge-interpretations/" + opened["interpretation_id"] + "/resume",
                               headers=api.headers, json=api.binding)
    assert response.status_code == 409
    assert not api.app.state.proposal_contexts._entries


def test_next_request_waits_until_cancelled_workers_lease_is_released(api, monkeypatch):
    entered, release = Event(), Event()
    original = api.sessions.release_context
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(api.sessions, "release_context", delayed)
    api.state.hold = True
    first = open_request(api)
    assert api.state.started.wait(3)
    second_binding = {**api.binding, "request_id": "second-request", "display_session_id": "second-display"}
    second = open_request(api, binding=second_binding)
    try:
        api.client.post("/v1/knowledge-interpretations/" + first["interpretation_id"] + "/cancel",
                         headers=api.headers, json=api.binding)
        assert entered.wait(3)
        assert len(api.state.calls) == 1
        api.state.hold = False
    finally:
        release.set()
    assert wait_result(api, second, second_binding)["state"] == "review_ready"
    assert len(api.state.calls) == 2


def test_lost_context_lease_stops_worker_instead_of_leaving_running_state(api, monkeypatch):
    api.sessions.lease_ttl_seconds = 0.3
    monkeypatch.setattr(api.sessions, "renew_context", lambda *args: False)
    api.state.hold = True
    opened = open_request(api)
    assert wait_result(api, opened)["state"] == "failed"
    assert api.state.calls[0][2]["cancel_event"].is_set()
    assert not api.app.state.proposal_contexts._entries


def test_normal_chat_cannot_run_while_interpretation_owns_canonical_context(api):
    api.state.hold = True
    opened = open_request(api)
    assert api.state.started.wait(3)
    try:
        response = api.client.post("/v1/chat", headers={**api.headers, "Idempotency-Key": "ordinary-query-123"},
                                   json={"conversation_id": api.binding["conversation_id"], "question": "查询这次销售数据"})
        assert response.status_code == 409
    finally:
        api.client.post("/v1/knowledge-interpretations/" + opened["interpretation_id"] + "/cancel",
                         headers=api.headers, json=api.binding)


def test_separate_installations_get_different_backend_contexts(api):
    first = open_request(api)
    assert wait_result(api, first)["state"] == "review_ready"
    api.client.cookies.clear()
    bootstrap = api.client.get("/v1/session").json()
    api.headers = {"X-CSRF-Token": bootstrap["csrf_token"]}
    conversation = api.client.post("/v1/conversations", headers=api.headers).json()["conversation_id"]
    api.binding = {**api.binding, "conversation_id": conversation}
    second = open_request(api)
    assert wait_result(api, second)["state"] == "review_ready"
    assert api.state.calls[0][2]["context_home"] != api.state.calls[1][2]["context_home"]
    with api.sessions._connection() as connection:
        rows = connection.execute("SELECT codex_thread_id FROM chat_principal_contexts_v1").fetchall()
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
