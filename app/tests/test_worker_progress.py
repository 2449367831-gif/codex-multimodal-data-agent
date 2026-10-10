import json
import subprocess

import pytest

from app.worker_progress import PREFIX, emit, phase, last_progress


def test_progress_disabled_outside_private_worker(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_CHAT_INTERNAL_RESULT_STDOUT", raising=False)
    emit("model", "started")
    assert capsys.readouterr().err == ""


def test_worker_events_have_only_bounded_operational_fields(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_CHAT_INTERNAL_RESULT_STDOUT", "1")
    with phase("query", step=3):
        pass
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert last_progress(lines[0])["event"] == "started"
    assert last_progress(lines[1])["event"] == "completed"
    with pytest.raises(ValueError, match="PRIVATE"):
        with phase("model"):
            raise ValueError("PRIVATE ERROR CONTENT")
    wire = capsys.readouterr().err
    assert "PRIVATE" not in wire and last_progress(wire)["event"] == "failed"


@pytest.mark.parametrize("field,value", [("phase", "SELECT private"), ("phase", []),
    ("event", "private"), ("step", True), ("step", -1), ("step", 129),
    ("elapsed_ms", "private"), ("elapsed_ms", -1), ("elapsed_ms", 3_600_001), ("sql", "private")])
def test_invalid_child_fields_never_reach_logs(field, value):
    record = {"phase": "model", "event": "started", "step": 1, "elapsed_ms": 3, field: value}
    assert last_progress(PREFIX + json.dumps(record)) is None


def test_parser_handles_partial_stderr_and_selects_last_valid_event():
    record = {"phase": "query", "event": "started", "step": 2, "elapsed_ms": 500}
    wire = "PRIVATE\n" + PREFIX + json.dumps(record) + "\n" + PREFIX + "{invalid}\nPRIVATE"
    assert last_progress(wire.encode()) == record
    assert last_progress({}) is None


def test_timeout_only_logs_whitelisted_progress(tmp_path, monkeypatch, caplog):
    from app.chat_api import ChatRunner, ChatTimeoutError
    runner = ChatRunner(project_root=tmp_path, timeout_seconds=10)
    monkeypatch.setattr(ChatRunner, "auth_configured", property(lambda _: True))
    record = {"phase": "model", "event": "started", "step": 1, "elapsed_ms": 200}
    def child(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 10, stderr=("PRIVATE\n" + PREFIX + json.dumps(record)).encode())
    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    with pytest.raises(ChatTimeoutError) as caught:
        runner.run("PRIVATE QUESTION")
    assert "phase=model event=started step=1 elapsed_ms=200" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert caught.value.details == {"reason": "process_timeout"}


def test_confirmed_model_budget_exhaustion_is_timeout_not_generic_error(tmp_path, monkeypatch):
    from app.chat_api import ChatRunner, ChatTimeoutError
    runner = ChatRunner(project_root=tmp_path, timeout_seconds=10)
    monkeypatch.setattr(ChatRunner, "auth_configured", property(lambda _: True))
    record = {"phase": "model", "event": "timeout", "step": 2, "elapsed_ms": 195000}
    calls = []
    def child(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "PRIVATE\n" + PREFIX + json.dumps(record))
    monkeypatch.setattr("app.chat_api.subprocess.run", child)
    with pytest.raises(ChatTimeoutError) as caught:
        runner.run("private question")
    assert len(calls) == 1
    assert caught.value.details == {"reason": "model_timeout"}
    assert "PRIVATE" not in str(caught.value)
