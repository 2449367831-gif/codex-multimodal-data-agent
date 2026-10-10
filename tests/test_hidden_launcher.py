from pathlib import Path
import importlib.util
import json
from unittest.mock import Mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_launcher_module():
    path = PROJECT_ROOT / "scripts" / "launch_hidden.py"
    spec = importlib.util.spec_from_file_location("hidden_launcher_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hidden_launcher_does_not_depend_on_powershell() -> None:
    launcher = (PROJECT_ROOT / "scripts" / "launch_hidden.py").read_text(encoding="utf-8")
    vbs = (PROJECT_ROOT / "scripts" / "start_hidden.vbs").read_text(encoding="utf-8")

    assert "powershell" not in launcher.casefold()
    assert "CREATE_NO_WINDOW" in launcher
    assert "CREATE_BREAKAWAY_FROM_JOB" in launcher
    assert "subprocess.STARTF_USESHOWWINDOW" in launcher
    assert "pythonw.exe" in vbs
    assert "shell.Run" in vbs
    assert ", 0, False" in vbs


def test_hidden_launcher_keeps_services_on_loopback() -> None:
    launcher = (PROJECT_ROOT / "scripts" / "launch_hidden.py").read_text(encoding="utf-8")

    assert '"127.0.0.1"' in launcher
    assert "--host" in launcher
    assert "http://127.0.0.1:" in launcher


def test_hidden_launcher_starts_a_single_restartable_supervisor() -> None:
    launcher = (PROJECT_ROOT / "scripts" / "launch_hidden.py").read_text(encoding="utf-8")

    assert "--supervise" in launcher
    assert "service-supervisor.lock" in launcher
    assert "CreateMutexW" in launcher
    assert "ERROR_ALREADY_EXISTS" in launcher
    assert "SUPERVISOR_RETRY_SECONDS" in launcher
    assert "SUPERVISOR_MAX_RETRY_SECONDS" in launcher
    assert "_retry_delay" in launcher
    assert "SERVICE_HEALTH_FAILURE_LIMIT" in launcher
    assert "_service_is_ready" in launcher
    assert "/live" in launcher
    assert "_terminate_managed_service" in launcher
    assert "_start_supervisor(environment)" in launcher


def test_hidden_launcher_does_not_inherit_stale_runtime_overrides() -> None:
    launcher = _load_launcher_module()

    selected = launcher._select_runtime_environment(
        {
            "PATH": "keep",
            "DORIS_HOST": "127.0.0.1",
            "AGENT_AUDIT_DB": "output/playwright/old.sqlite3",
        },
        {"DORIS_HOST": "doris-fe.internal", "DORIS_PORT": "9030"},
        {},
    )

    assert selected["PATH"] == "keep"
    assert selected["DORIS_HOST"] == "doris-fe.internal"
    assert selected["DORIS_PORT"] == "9030"
    assert "AGENT_AUDIT_DB" not in selected


def test_hidden_launcher_project_file_wins_over_user_environment() -> None:
    launcher = _load_launcher_module()

    selected = launcher._select_runtime_environment(
        {"DORIS_HOST": "inherited"},
        {"DORIS_HOST": "user-setting"},
        {"DORIS_HOST": "project-setting"},
    )

    assert selected["DORIS_HOST"] == "project-setting"


def test_model_and_task_budgets_use_explicit_deployment_values():
    launcher = _load_launcher_module()
    values = {"AGENT_CHAT_TASK_TIMEOUT_SECONDS": "480", "AGENT_MODEL_TURN_TIMEOUT_SECONDS": "120",
              "AGENT_MODEL_RETRY_TIMEOUT_SECONDS": "75"}
    assert set(values) <= launcher.RUNTIME_CONFIG_KEYS
    assert launcher._select_runtime_environment({}, {}, values) == values
    assert launcher._select_runtime_environment(values, {}, {}) == {}


def test_live_probe_is_independent_of_dependency_readiness(monkeypatch) -> None:
    launcher = _load_launcher_module()
    monkeypatch.setattr(launcher, "_port_is_open", lambda _: True)
    requested = []

    def open_probe(request, *, timeout):
        requested.append(request.full_url)
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"ready": request.full_url.endswith("/live")}).encode()
        return response

    monkeypatch.setattr(launcher.urllib.request, "build_opener", lambda *args: Mock(open=open_probe))
    assert launcher._service_is_live(8021)
    assert not launcher._service_is_ready(8021)
    assert requested == ["http://127.0.0.1:8021/live", "http://127.0.0.1:8021/health"]


def test_supervisor_keeps_live_service_when_doris_is_unavailable(monkeypatch) -> None:
    launcher = _load_launcher_module()
    monkeypatch.setattr(launcher, "_SupervisorLock", lambda _: Mock(acquire=lambda: True))
    monkeypatch.setattr(launcher, "_configure_environment", lambda: ({}, 8010, 8021))
    monkeypatch.setattr(launcher, "_log", lambda _: None)
    tick = [0]
    monkeypatch.setattr(launcher.time, "monotonic", lambda: 100.0 + tick[0] * 60)
    monkeypatch.setattr(launcher, "_port_is_open", lambda _: tick[0] > 0)
    started = Mock(side_effect=[101, 102])
    stopped = Mock()
    live = Mock(return_value=True)
    ready = Mock(side_effect=AssertionError("watchdog must not probe external dependencies"))
    monkeypatch.setattr(launcher, "_start_service", started)
    monkeypatch.setattr(launcher, "_terminate_managed_service", stopped)
    monkeypatch.setattr(launcher, "_service_is_live", live)
    monkeypatch.setattr(launcher, "_service_is_ready", ready)

    def tick_forward(_):
        tick[0] += 1
        if tick[0] > launcher.SERVICE_HEALTH_FAILURE_LIMIT + 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(launcher.time, "sleep", tick_forward)
    assert launcher._run_supervisor() == 0
    assert started.call_count == 2
    assert live.call_count > launcher.SERVICE_HEALTH_FAILURE_LIMIT
    stopped.assert_not_called()
    ready.assert_not_called()


@pytest.mark.parametrize("body", [b"", b"not-json", b"[]", b'{"ready":false}'])
def test_invalid_live_response_cannot_claim_a_healthy_process(monkeypatch, body):
    launcher = _load_launcher_module()
    monkeypatch.setattr(launcher, "_port_is_open", lambda _: True)
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = body
    monkeypatch.setattr(launcher.urllib.request, "build_opener", lambda *args: Mock(open=lambda *a, **k: response))
    assert not launcher._service_is_live(8021)
