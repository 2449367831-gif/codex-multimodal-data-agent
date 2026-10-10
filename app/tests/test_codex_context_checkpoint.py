from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.codex_context_checkpoint import ContextCheckpoint, ContextCheckpointError
from app.codex_interpretation import InterpretationRunError, run_codex_interpretation
from app.chat_api import ChatRunner, ChatExecutionError, ChatTimeoutError
from app.chat_sessions import ChatSessionStore, Principal
from app.tests.test_codex_interpretation import runtime
from app.tests.test_proposal_interpretation import QUESTION
from app.tests.test_proposal_wire import wire_draft
from app.tests.test_proposal_interpretation import draft


def test_creation_claim_and_ready_identity_survive_new_store_instance(tmp_path):
    checkpoint = ContextCheckpoint(tmp_path / "owner")
    assert checkpoint.resolve() is None
    checkpoint.claim_creation()
    with pytest.raises(ContextCheckpointError, match="context_initialization_uncertain"):
        ContextCheckpoint(checkpoint.home).resolve()
    checkpoint.bind("thread-canonical-123")
    assert ContextCheckpoint(checkpoint.home).resolve() == "thread-canonical-123"
    assert checkpoint.resolve("thread-canonical-123") == "thread-canonical-123"
    with pytest.raises(ContextCheckpointError, match="context_checkpoint_mismatch"):
        checkpoint.resolve("thread-other-456")
    with pytest.raises(ContextCheckpointError, match="context_checkpoint_mismatch"):
        checkpoint.bind("thread-other-456")
    assert checkpoint.resolve() == "thread-canonical-123"
    assert not list(checkpoint.home.glob("*.tmp"))


def test_concurrent_creation_has_one_winner(tmp_path):
    home = tmp_path / "owner"
    def claim(_):
        try:
            ContextCheckpoint(home).claim_creation()
            return True
        except ContextCheckpointError:
            return False
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(claim, range(24))) == 1


@pytest.mark.parametrize("corruption", ["truncated", "duplicate_key", "extra", "wrong_partition", "wrong_version", "oversized", "wrong_id"])
def test_invalid_checkpoint_never_becomes_a_new_context(tmp_path, corruption):
    checkpoint = ContextCheckpoint(tmp_path)
    checkpoint.bind("thread-canonical-123")
    value = json.loads(checkpoint.ready_path.read_text())
    raw = None
    if corruption == "truncated": raw = '{"version":'
    if corruption == "duplicate_key": raw = '{"version":1,"version":1}'
    if corruption == "extra": value["question"] = "must-not-store"
    if corruption == "wrong_partition": value["partition"] = "0" * 64
    if corruption == "wrong_version": value["version"] = True
    if corruption == "oversized": raw = " " * 2049
    if corruption == "wrong_id": value["thread_id"] = "bad"
    checkpoint.ready_path.write_text(raw or json.dumps(value), encoding="utf-8")
    with pytest.raises(ContextCheckpointError, match="context_checkpoint_invalid"):
        checkpoint.resolve()
    with pytest.raises(ContextCheckpointError):
        checkpoint.claim_creation()


def test_copied_checkpoint_cannot_bind_another_partition(tmp_path):
    first, second = ContextCheckpoint(tmp_path / "a"), ContextCheckpoint(tmp_path / "b")
    first.bind("thread-canonical-123")
    second.home.mkdir()
    second.ready_path.write_bytes(first.ready_path.read_bytes())
    with pytest.raises(ContextCheckpointError, match="context_checkpoint_invalid"):
        second.resolve()


def test_checkpoint_contains_only_identity_state_not_prompts_or_results(tmp_path):
    checkpoint = ContextCheckpoint(tmp_path)
    checkpoint.claim_creation()
    checkpoint.bind("thread-canonical-123")
    for path in (checkpoint.claim_path, checkpoint.ready_path):
        value = json.loads(path.read_text())
        assert set(value) == {"version", "partition", "state", "thread_id"}


def test_failed_first_interpretation_recovers_without_creating_another_thread(runtime):
    runtime.kwargs["thread_id"] = None
    runtime.raw = "invalid model response"
    with pytest.raises(InterpretationRunError):
        run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    runtime.raw = json.dumps(wire_draft(draft(runtime.snapshot)), ensure_ascii=False)
    result = run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert result["context_reused"] and result["thread_id"] == "canonical-thread-123"
    assert sum(call[0] == "start" for call in runtime.calls) == 1
    assert sum(call[0] == "resume" for call in runtime.calls) == 1


def test_uncertain_sdk_creation_is_not_automatically_repeated(runtime, monkeypatch):
    import openai_codex
    runtime.kwargs["thread_id"] = None
    starts = []
    def fail(*args, **kwargs):
        starts.append(True)
        raise RuntimeError("creation response lost")
    monkeypatch.setattr(openai_codex.Codex, "thread_start", fail)
    with pytest.raises(InterpretationRunError):
        run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    with pytest.raises(InterpretationRunError, match="context_initialization_uncertain"):
        run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert len(starts) == 1
    assert not any(call[0] == "run" for call in runtime.calls)


@pytest.fixture
def runner_context(tmp_path):
    runner = ChatRunner(project_root=tmp_path, timeout_seconds=10)
    runner.codex_home.mkdir(parents=True)
    (runner.codex_home / "auth.json").write_text("{}")
    store = ChatSessionStore(tmp_path / "sessions.sqlite3")
    session = store.ensure_session(None, None, Principal("installation", "synthetic-machine"))
    home = runner.context_home_for_ref(store.context_home_ref(session))
    def bind(thread_id):
        return store.bind_context_thread(session, thread_id)
    return runner, store, session, home, bind


def test_parent_binds_created_thread_even_when_child_times_out(runner_context, monkeypatch):
    runner, store, session, home, bind = runner_context
    def child(command, **kwargs):
        checkpoint = ContextCheckpoint(home)
        checkpoint.claim_creation()
        checkpoint.bind("thread-before-timeout")
        raise subprocess.TimeoutExpired(command, 10)
    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    with pytest.raises(ChatTimeoutError):
        runner.run("synthetic request", context_home=home, on_context_ready=bind)
    assert store.context(session).codex_thread_id == "thread-before-timeout"
    assert store.context(session).turn_count == 0
    assert not list(runner.internal_result_root.glob("*.json"))


def test_retry_and_subsequent_request_resume_checkpoint_identity(runner_context, monkeypatch):
    runner, store, session, home, bind = runner_context
    commands = []
    def child(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            ContextCheckpoint(home).claim_creation()
            ContextCheckpoint(home).bind("thread-before-failure")
        return subprocess.CompletedProcess(command, 1, "", "transient" if len(commands) == 1 else "terminal")
    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    monkeypatch.setattr("app.chat_api._is_transient_child_failure", lambda error: error == "transient")
    monkeypatch.setattr("app.chat_api.CHAT_CHILD_RETRY_DELAY_SECONDS", 0)
    with pytest.raises(ChatExecutionError):
        runner.run("synthetic request", context_home=home, on_context_ready=bind)
    assert len(commands) == 2
    assert "--thread-id" not in commands[0]
    assert commands[1][commands[1].index("--thread-id") + 1] == "thread-before-failure"
    assert store.context(session).codex_thread_id == "thread-before-failure"
    with pytest.raises(ChatExecutionError):
        runner.run("new request", context_home=home, on_context_ready=bind)
    assert commands[-1][commands[-1].index("--thread-id") + 1] == "thread-before-failure"


def test_parent_does_not_launch_worker_for_uncertain_creation(runner_context, monkeypatch):
    runner, store, session, home, bind = runner_context
    ContextCheckpoint(home).claim_creation()
    monkeypatch.setattr("app.chat_api.subprocess.run", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ChatExecutionError) as error:
        runner.run("synthetic request", context_home=home, on_context_ready=bind)
    assert error.value.details["reason"] == "context_initialization_uncertain"
    assert store.context(session).codex_thread_id is None


def test_context_lock_blocks_another_process_and_releases_after_exit(tmp_path):
    checkpoint = ContextCheckpoint(tmp_path / "owner")
    program = (
        "import sys\nfrom app.codex_context_checkpoint import ContextCheckpoint, ContextCheckpointError\n"
        "try:\n with ContextCheckpoint(sys.argv[1]).lock(): print('acquired')\n"
        "except ContextCheckpointError as error: print(str(error))\n"
    )
    def probe():
        return subprocess.run([sys.executable, "-c", program, str(checkpoint.home)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout.strip()
    with checkpoint.lock():
        assert probe() == "context_worker_busy"
        with ContextCheckpoint(tmp_path / "different-owner").lock():
            pass
    assert probe() == "acquired"


def test_chat_api_retains_identity_after_failed_first_request(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.chat_api import create_chat_app, IdentityResolver
    runner = ChatRunner(project_root=tmp_path, timeout_seconds=10)
    runner.codex_home.mkdir(parents=True)
    (runner.codex_home / "auth.json").write_text("{}")
    store = ChatSessionStore(tmp_path / "sessions.sqlite3")
    commands = []
    def child(command, **kwargs):
        commands.append(command)
        home = Path(command[command.index("--context-home") + 1])
        if len(commands) == 1:
            ContextCheckpoint(home).claim_creation()
            ContextCheckpoint(home).bind("thread-api-first-failure")
        raise subprocess.TimeoutExpired(command, 10)
    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    monkeypatch.setattr("app.chat_api._read_backend_health", lambda _: {"ready": True})
    monkeypatch.setattr("app.chat_api.AgentOrchestrator", lambda *a, **k: SimpleNamespace(run_if_supported=lambda *a, **k: None))
    monkeypatch.setattr("app.chat_api.reviewed_clarification_for_question", lambda _: None)
    with TestClient(create_chat_app(runner, session_store=store, identity_resolver=IdentityResolver(mode="anonymous"),
                                   enforce_loopback=False)) as client:
        bootstrap = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": bootstrap["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        for index in range(2):
            response = client.post("/v1/chat", headers={**headers, "Idempotency-Key": f"checkpoint-request-{index}"},
                                   json={"question": "查询本次业务数据", "conversation_id": conversation})
            assert response.status_code == 504
            assert "thread-api-first-failure" not in response.text
        with store._connection() as connection:
            rows = connection.execute(f"SELECT codex_thread_id,turn_count FROM {store._contexts_table}").fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == ("thread-api-first-failure", 0)
        assert len(commands) == 2
        assert "--thread-id" not in commands[0]
        assert commands[1][commands[1].index("--thread-id") + 1] == "thread-api-first-failure"
