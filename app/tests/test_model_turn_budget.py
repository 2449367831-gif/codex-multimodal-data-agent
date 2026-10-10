from threading import Event
from types import SimpleNamespace

import pytest

from app.model_turn_budget import run_turn, ModelTurnCancellationUnconfirmed


@pytest.mark.parametrize("configured,wanted", [("0", 5), ("9999", 300), ("invalid", 180), ("100", 100)])
def test_attempt_budgets_are_always_bounded(monkeypatch, configured, wanted):
    from app.model_turn_budget import attempt_budget
    monkeypatch.setenv("AGENT_MODEL_TURN_TIMEOUT_SECONDS", configured)
    monkeypatch.delenv("AGENT_MODEL_RETRY_TIMEOUT_SECONDS", raising=False)
    assert attempt_budget(0) == wanted
    assert attempt_budget(1) == 90


class Handle:
    def __init__(self, status="completed", wait=False, terminal=True, rpc=True):
        self.status, self.terminal, self.rpc = status, terminal, rpc
        self.release = Event()
        if not wait:
            self.release.set()
        self.cancel_release = Event()
        if rpc:
            self.cancel_release.set()
        self.interruptions = 0
        self.finished = Event()

    def run(self):
        self.release.wait()
        self.finished.set()
        return SimpleNamespace(status=self.status)

    def interrupt(self):
        self.interruptions += 1
        if self.terminal:
            self.release.set()
        self.cancel_release.wait()


class Conversation:
    def __init__(self, handle):
        self.handle = handle
        self.calls = []

    def turn(self, prompt, **options):
        self.calls.append((prompt, options))
        return self.handle


def execute(handle):
    return run_turn(Conversation(handle), "private question", effort="xhigh", model="gpt-5.6-sol",
                    sandbox="read_only", timeout_seconds=0.02, cancel_grace_seconds=0.05)


def test_completed_turn_uses_public_sdk_handle_without_interrupt():
    handle = Handle()
    assert execute(handle).status == "completed"
    assert not handle.interruptions


def test_budget_raises_retryable_timeout_only_after_interrupted_terminal_event():
    handle = Handle("interrupted", wait=True)
    with pytest.raises(TimeoutError, match="model_turn_budget_exhausted"):
        execute(handle)
    assert handle.finished.is_set() and handle.interruptions == 1


def test_completed_result_wins_timeout_race():
    handle = Handle("completed", wait=True)
    assert execute(handle).status == "completed"
    assert handle.interruptions == 1


@pytest.mark.parametrize("terminal,rpc,status", [(False, True, "interrupted"),
                                                 (True, False, "interrupted"),
                                                 (True, True, "inProgress")])
def test_uncertain_cancellation_never_authorizes_retry(terminal, rpc, status):
    from app.codex_agent import _is_transient_codex_error
    handle = Handle(status, wait=True, terminal=terminal, rpc=rpc)
    try:
        with pytest.raises(ModelTurnCancellationUnconfirmed) as error:
            execute(handle)
        assert not _is_transient_codex_error(error.value)
        assert str(error.value) == "model_turn_cancellation_unconfirmed"
    finally:
        handle.release.set()
        handle.cancel_release.set()


def test_transport_error_is_preserved_before_deadline():
    handle = Handle()
    def fail():
        raise ConnectionError("private transport detail")
    handle.run = fail
    with pytest.raises(ConnectionError):
        execute(handle)
    assert not handle.interruptions


def test_lifecycle_recovery_cannot_restart_an_unconfirmed_turn():
    handle = Handle("interrupted", wait=True, terminal=False)
    conversation = Conversation(handle)
    try:
        for _ in range(2):
            with pytest.raises(ModelTurnCancellationUnconfirmed):
                run_turn(conversation, "question", effort="xhigh", model="gpt-5.6-sol", sandbox="read_only",
                         timeout_seconds=0.01, cancel_grace_seconds=0.01)
        assert len(conversation.calls) == 1
    finally:
        handle.release.set()


def test_production_retry_stays_in_same_conversation_after_confirmed_stop(monkeypatch):
    from app import codex_agent
    from openai_codex.generated.v2_all import ReasoningEffort
    original = run_turn
    monkeypatch.setattr(codex_agent, "bounded_model_turn", lambda thread, prompt, **options:
        original(thread, prompt, **{**options, "timeout_seconds": 0.02, "cancel_grace_seconds": 0.05}))
    monkeypatch.setattr(codex_agent, "CODEX_TURN_RETRY_DELAY_SECONDS", 0)
    first, second = Handle("interrupted", wait=True), Handle()
    class SerialConversation(Conversation):
        def turn(self, prompt, **options):
            if self.calls:
                assert first.finished.is_set()
                self.handle = second
            return super().turn(prompt, **options)
    conversation = SerialConversation(first)
    result = codex_agent._run_codex_turn(conversation, "request", effort=ReasoningEffort.xhigh,
                                         model="gpt-5.6-sol", sandbox="read_only")
    assert result.status == "completed"
    assert [call[1]["effort"].value for call in conversation.calls] == ["xhigh", "high"]
    assert [call[1]["model"] for call in conversation.calls] == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert len(conversation.calls) == 2


def test_confirmed_timeout_retry_keeps_request_but_does_not_repeat_knowledge(monkeypatch):
    from app import codex_agent
    from app.model_turn_budget import ModelTurnBudgetExceeded
    calls = []

    def bounded(thread, prompt, **options):
        calls.append((thread, prompt))
        if len(calls) == 1:
            raise ModelTurnBudgetExceeded("model_turn_budget_exhausted")
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(codex_agent, "bounded_model_turn", bounded)
    monkeypatch.setattr(codex_agent, "CODEX_TURN_RETRY_DELAY_SECONDS", 0)
    thread = object()
    prompt = 'current question\n\n[HOST_REQUEST_KNOWLEDGE]\n{"release":"current"}'
    codex_agent._run_codex_turn(thread, prompt, effort="xhigh", model="gpt-5.6-sol", sandbox="read_only")
    assert calls[0] == (thread, prompt)
    assert calls[1][0] is thread
    assert "current question" in calls[1][1]
    assert "HOST_CONFIRMED_TIMEOUT_RETRY" in calls[1][1]
    assert '{"release":"current"}' not in calls[1][1]


def test_transport_retry_resends_knowledge_when_request_acceptance_is_unknown(monkeypatch):
    from app import codex_agent
    calls = []

    def bounded(thread, prompt, **options):
        calls.append(prompt)
        if len(calls) == 1:
            raise ConnectionError("connection reset")
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(codex_agent, "bounded_model_turn", bounded)
    monkeypatch.setattr(codex_agent, "CODEX_TURN_RETRY_DELAY_SECONDS", 0)
    prompt = 'question\n\n[HOST_REQUEST_KNOWLEDGE]\n{"release":"current"}'
    codex_agent._run_codex_turn(object(), prompt, effort="xhigh", model="gpt-5.6-sol", sandbox="read_only")
    assert calls == [prompt, prompt]


def test_planning_and_answer_instructions_have_one_execution_owner():
    from app.codex_agent import CODEX_DEVELOPER_INSTRUCTIONS, HOST_PLAN_EXECUTION_INSTRUCTIONS
    assert "Return private query-plan JSON for the host" in HOST_PLAN_EXECUTION_INSTRUCTIONS
    assert "do not attempt native tools" in HOST_PLAN_EXECUTION_INSTRUCTIONS
    assert "Do not transcribe result numbers" in CODEX_DEVELOPER_INSTRUCTIONS
    assert "The answer must contain the requested business values" not in CODEX_DEVELOPER_INSTRUCTIONS
