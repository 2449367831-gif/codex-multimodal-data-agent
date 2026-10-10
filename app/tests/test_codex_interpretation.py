from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
import yaml

from app import codex_interpretation as adapter
from app.tests.test_knowledge_proposals import baseline
from app.tests.test_proposal_interpretation import QUESTION, draft
from app.tests.test_proposal_wire import wire_draft


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    snapshot = baseline()
    _, restrictions = adapter._configuration(tmp_path)
    state = SimpleNamespace(calls=[], restrictions=restrictions, resume_error=False,
                            respect_overrides=False,
                            returned_id="canonical-thread-123", status="completed", error=None,
                            items=[], raw=json.dumps(wire_draft(draft(snapshot)), ensure_ascii=False))

    class Thread:
        id = state.returned_id

        def run(self, prompt, **kwargs):
            state.calls.append(("run", prompt, kwargs))
            return SimpleNamespace(id="turn-123", status=state.status, error=state.error,
                                   items=state.items, final_response=state.raw)

    class Codex:
        def __init__(self, config):
            state.calls.append(("client", config))
            self._client = self
            self.config = config

        def __enter__(self): return self
        def __exit__(self, *args): state.calls.append(("close",))

        def request(self, method, params, **kwargs):
            state.calls.append((method, params))
            effective = deepcopy(state.restrictions)
            if state.respect_overrides:
                for name, server in effective["mcp_servers"].items():
                    if f"mcp_servers.{name}.enabled=false" in self.config.config_overrides:
                        server.update(enabled=False, required=False, enabled_tools=[])
            return SimpleNamespace(config=effective)

        def thread_resume(self, thread_id, **kwargs):
            state.calls.append(("resume", thread_id, kwargs))
            if state.resume_error:
                raise RuntimeError("PRIVATE_PROVIDER_DETAIL")
            result = Thread()
            result.id = state.returned_id
            return result

        def thread_start(self, **kwargs):
            state.calls.append(("start", kwargs))
            return Thread()

    @contextmanager
    def home(source, **kwargs):
        yield kwargs["persistent_home"]

    import openai_codex
    monkeypatch.setattr(openai_codex, "Codex", Codex)
    monkeypatch.setattr(adapter, "isolated_codex_home", home)
    monkeypatch.setattr(adapter, "assert_codex_environment_isolated", lambda: None)
    monkeypatch.setattr(adapter, "_hide_windows_subprocesses", lambda: None)
    state.snapshot = snapshot
    state.kwargs = {"project_root": tmp_path, "context_home": tmp_path / "var/codex-contexts" / ("a" * 64),
                    "thread_id": "canonical-thread-123"}
    return state


def test_resume_uses_same_thread_and_no_query_configuration(runtime):
    result = adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert result["thread_id"] == runtime.kwargs["thread_id"]
    assert result["context_reused"] and not result["context_reset"] and not result["query_executed"]
    assert result["result_state"] == "definition_interpretation"
    assert [call[0] for call in runtime.calls] == ["client", "config/read", "resume", "run", "close"]
    resume = runtime.calls[2][2]
    assert resume["config"]["mcp_servers"] == {}
    assert resume["config"]["features"]["shell_tool"] is False
    assert resume["config"]["features"]["plugins"] is False
    assert resume["approval_mode"].value == "deny_all"
    overrides = runtime.calls[0][1].config_overrides
    assert "mcp_servers={}" in overrides
    assert not any("app.mcp_server" in value for value in overrides)
    assert result["model"] == "gpt-6" and result["reasoning_effort"] == "low"


def test_initial_thread_is_created_once_and_identity_survives_invalid_output(runtime):
    runtime.kwargs["thread_id"] = None
    runtime.raw = "PRIVATE_BAD_RESPONSE"
    with pytest.raises(adapter.InterpretationRunError) as error:
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert error.value.thread_id == "canonical-thread-123"
    assert error.value.turn_id == "turn-123"
    assert "PRIVATE" not in str(error.value)
    assert sum(call[0] == "start" for call in runtime.calls) == 1
    assert sum(call[0] == "run" for call in runtime.calls) == 1


def test_resume_failure_does_not_create_replacement_or_query(runtime):
    runtime.resume_error = True
    with pytest.raises(adapter.InterpretationRunError, match="interpretation_resume_failed") as error:
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert error.value.thread_id == runtime.kwargs["thread_id"]
    assert not any(call[0] in {"start", "run"} for call in runtime.calls)


def test_mismatched_resume_is_rejected_before_model_turn(runtime):
    runtime.returned_id = "another-thread-123"
    with pytest.raises(adapter.InterpretationRunError, match="interpretation_thread_mismatch") as error:
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert error.value.thread_id == runtime.kwargs["thread_id"]
    assert not any(call[0] in {"start", "run"} for call in runtime.calls)


@pytest.mark.parametrize("alter", ["mcp", "shell", "missing", "string_false", "web"])
def test_effective_config_must_prove_restrictions_before_thread_start(runtime, alter):
    if alter == "mcp": runtime.restrictions["mcp_servers"] = {"unexpected": {"command": "never-run"}}
    if alter == "shell": runtime.restrictions["features"]["shell_tool"] = True
    if alter == "missing": del runtime.restrictions["features"]["shell_tool"]
    if alter == "string_false": runtime.restrictions["features"]["shell_tool"] = "false"
    if alter == "web": runtime.restrictions["web_search"] = "live"
    with pytest.raises(adapter.InterpretationRunError, match="interpretation_tools_not_disabled"):
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert not any(call[0] in {"start", "resume", "run"} for call in runtime.calls)


@pytest.mark.parametrize("kind", ["mcpToolCall", "commandExecution", "fileChange", "webSearch", "unknownFutureAction"])
def test_tool_actions_are_not_mistaken_for_successful_interpretation(runtime, kind):
    runtime.items = [SimpleNamespace(root=SimpleNamespace(type=kind))]
    with pytest.raises(adapter.InterpretationRunError, match="interpretation_unexpected_action") as error:
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert error.value.turn_id == "turn-123" and error.value.thread_id == runtime.kwargs["thread_id"]
    assert sum(call[0] == "run" for call in runtime.calls) == 1


@pytest.mark.parametrize("status", ["failed", "interrupted", "inProgress", None])
def test_nonterminal_and_failed_results_never_become_candidates(runtime, status):
    runtime.status = status
    with pytest.raises(adapter.InterpretationRunError, match="interpretation_turn_incomplete"):
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)


@pytest.mark.parametrize("path", [None, "outside", "var/codex-contexts", "var/codex-contexts/raw-user", "var/codex-contexts/aaaaaaaaaaaaaaaa/child"])
def test_context_requires_one_server_hashed_partition(runtime, path):
    runtime.kwargs["context_home"] = runtime.kwargs["project_root"] / path if path else None
    with pytest.raises(adapter.InterpretationRunError):
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert runtime.calls == []


def test_prompt_preserves_exact_decimal_contract_and_current_request(runtime):
    runtime.snapshot.contracts["metric:inventory.available"]["parameters"] = {
        "ratio": {"required": False, "default": Decimal("0.1234567890123456789")}}
    runtime.raw = json.dumps(wire_draft(draft(runtime.snapshot)), ensure_ascii=False)
    adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    prompt = next(call[1] for call in runtime.calls if call[0] == "run")
    assert "0.1234567890123456789" in prompt
    parsed = yaml.safe_load(prompt.split("PRIVATE_INTERPRETATION_INPUT_YAML:\n", 1)[1])
    assert parsed["question"] == QUESTION
    assert len(parsed["contracts"]) == len(runtime.snapshot.contracts)
    assert parsed["policy"]["omitted_scope"] == "ALL"
    assert "history" not in parsed and "tools" not in parsed


def test_inherited_servers_are_disabled_individually_before_a_single_resume(runtime):
    runtime.respect_overrides = True
    runtime.restrictions["mcp_servers"] = {
        "other_server": {"command": "never-run", "enabled": True},
        "second-server": {"command": "never-run-either"},
    }
    result = adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert result["context_reused"]
    assert sum(call[0] == "client" for call in runtime.calls) == 2
    assert sum(call[0] == "resume" for call in runtime.calls) == 1
    assert sum(call[0] == "run" for call in runtime.calls) == 1
    assert not any(call[0] == "start" for call in runtime.calls)
    config = next(call[2]["config"] for call in runtime.calls if call[0] == "resume")
    assert all(server == {"enabled": False, "required": False, "enabled_tools": []}
               for server in config["mcp_servers"].values())


@pytest.mark.parametrize("name", ['has.dot', '"quoted"', 'line\nbreak', '', 'x' * 201])
def test_unsupported_inherited_names_fail_before_any_model_turn(runtime, name):
    runtime.restrictions["mcp_servers"] = {name: {"command": "never-run"}}
    with pytest.raises(adapter.InterpretationRunError, match="interpretation_tools_not_disabled"):
        adapter.run_codex_interpretation(QUESTION, runtime.snapshot, **runtime.kwargs)
    assert not any(call[0] in {"resume", "start", "run"} for call in runtime.calls)


def test_sdk_config_does_not_discard_legacy_fields_before_verification():
    parsed = adapter._EffectiveConfig.model_validate({"config": {
        "tools": {"view_image": False}, "mcp_servers": {}, "features": {"shell_tool": False},
    }})
    assert parsed.config["tools"]["view_image"] is False
