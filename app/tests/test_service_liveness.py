from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.query_backend import create_query_backend
from app.tests.test_chat_api import FakeChatRunner


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr("app.chat_api._read_backend_health", lambda _: {"ready": False})
    runner = FakeChatRunner()
    runner.public_health = lambda backend: {"ready": bool(backend and backend.get("ready")), "status": "degraded"}
    app = create_chat_app(
        runner, session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False,
    )
    with TestClient(app) as client:
        yield app, client


def test_chat_liveness_is_not_doris_readiness_and_health_alias_matches(chat):
    _, client = chat
    alive = client.get("/live")
    health = client.get("/health")
    versioned = client.get("/v1/health")

    assert alive.status_code == 200
    assert alive.json() == {"ready": True, "status": "ok", "service": "chat"}
    assert health.status_code == versioned.status_code == 200
    assert health.json() == versioned.json()
    assert health.json()["ready"] is False


@pytest.mark.parametrize("component", ["backend", "knowledge", "sessions"])
def test_chat_liveness_responds_during_slow_deep_health_check(chat, monkeypatch, component):
    app, client = chat
    entered, release = Event(), Event()

    def slow_health(*args):
        entered.set()
        assert release.wait(5), "test did not release the delayed health probe"
        return {"ready": False, "status": "unavailable"}

    if component == "backend":
        monkeypatch.setattr("app.chat_api._read_backend_health", slow_health)
    else:
        target = app.state.knowledge_base if component == "knowledge" else app.state.chat_session_store
        monkeypatch.setattr(target, "health", slow_health)
    with ThreadPoolExecutor(max_workers=2) as pool:
        health = pool.submit(client.get, "/health")
        try:
            assert entered.wait(2)
            alive = pool.submit(client.get, "/live").result(timeout=1)
            assert alive.status_code == 200 and alive.json()["ready"] is True
            assert not health.done()
        finally:
            release.set()
        assert health.result(timeout=3).status_code == 200


def test_backend_live_does_not_call_doris_or_health():
    service = Mock()
    service.health.return_value = {"ready": False}
    with TestClient(create_query_backend(service)) as client:
        assert client.get("/live").json() == {"ready": True, "status": "ok", "service": "query_backend"}
        service.health.assert_not_called()
        service.query.assert_not_called()
        assert client.get("/health").json()["ready"] is False


def test_live_keeps_backend_loopback_restriction():
    with TestClient(create_query_backend(Mock()), client=("192.0.2.10", 1234)) as client:
        assert client.get("/live").status_code == 403
