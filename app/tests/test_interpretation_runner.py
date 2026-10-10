from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic
from threading import Event, Timer

import pytest

from app import interpretation_runner as module
from app.codex_context_checkpoint import ContextCheckpoint
from app.codex_interpretation import InterpretationRunError
from app.interpretation_runner import InterpretationRunner, _exchange
from app.interpretation_worker import interpret_request
from app.private_worker_protocol import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, decode_private, encode_private
from app.proposal_interpretation import validate_interpretation
from app.tests.test_knowledge_proposals import baseline
from app.tests.test_proposal_interpretation import QUESTION, draft

ROOT = Path(__file__).resolve().parents[2]


def exchange_program(code, data=b"", timeout=5):
    return _exchange([sys.executable, "-c", code], data, cwd=ROOT, timeout_seconds=timeout)


def test_real_pipe_roundtrip_is_binary_and_handles_more_than_pipe_buffer():
    data = b"0123456789" * 50000
    assert exchange_program("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", data) == data


@pytest.mark.parametrize("program,reason", [
    ("import sys; sys.stderr.write('private diagnostics'); sys.exit(2)", "interpretation_worker_failed"),
    ("import sys; sys.stdout.buffer.write(b'x'*4000001)", "interpretation_output_too_large"),
    ("import sys; sys.stderr.buffer.write(b'x'*65537)", "interpretation_output_too_large"),
])
def test_real_pipe_errors_are_bounded_and_do_not_expose_output(program, reason):
    with pytest.raises(InterpretationRunError, match=f"^{reason}$"):
        exchange_program(program)


@pytest.mark.parametrize("blocked_input", [False, True])
def test_timeout_covers_blocked_output_and_input_and_reaps_worker(blocked_input, monkeypatch):
    processes = []
    original = module.subprocess.Popen
    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", record)
    started = monotonic()
    with pytest.raises(InterpretationRunError, match="^interpretation_timeout$"):
        exchange_program("import time; time.sleep(60)", b"x" * 1_000_000 if blocked_input else b"", timeout=0.2)
    assert monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None
    assert all(stream.closed for stream in (processes[0].stdin, processes[0].stdout, processes[0].stderr))


def test_real_worker_rejects_bad_input_without_sdk_or_private_traceback():
    output = _exchange([sys.executable, "-m", "app.interpretation_worker", "--project-root", str(ROOT)],
                       b"not-json-secret", cwd=ROOT, timeout_seconds=15)
    assert decode_private(output, limit=MAX_OUTPUT_BYTES) == {"ok": False, "error": "interpretation_worker_failed"}
    assert b"secret" not in output


def test_explicit_cancellation_interrupts_pipe_wait_without_waiting_for_deadline():
    cancelled = Event()
    timer = Timer(0.2, cancelled.set)
    timer.start()
    started = monotonic()
    try:
        with pytest.raises(InterpretationRunError, match="^interpretation_cancelled$"):
            _exchange([sys.executable, "-c", "import time; time.sleep(60)"], b"", cwd=ROOT,
                      timeout_seconds=30, cancel_event=cancelled)
        assert monotonic() - started < 5
    finally:
        timer.cancel()


@pytest.mark.skipif(os.name != "nt", reason="Windows process lifetime")
def test_timeout_kills_job_descendant_and_releases_context_lock(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    original = module.subprocess.Popen
    class ObservedOutput:
        def __init__(self, stream): self.stream = stream
        def read1(self, size):
            block = self.stream.read1(size)
            if block:
                handles.append(kernel.OpenProcess(0x100001, False, int(block.strip())))
            return block
        def close(self): self.stream.close()
    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        process.stdout = ObservedOutput(process.stdout)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", record)
    program = (
        "import subprocess,sys,time\n"
        "from app.worker_lifetime import retain_windows_worker_job\n"
        "from app.codex_context_checkpoint import ContextCheckpoint\n"
        "retain_windows_worker_job()\n"
        f"with ContextCheckpoint({str(tmp_path)!r}).lock():\n"
        " child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)\n"
        " print(child.pid,flush=True)\n"
        " time.sleep(60)\n"
    )
    try:
        with pytest.raises(InterpretationRunError, match="^interpretation_timeout$"):
            exchange_program(program, timeout=2)
        assert len(handles) == 1 and handles[0]
        assert kernel.WaitForSingleObject(handles[0], 5000) == 0
        with ContextCheckpoint(tmp_path).lock(): pass
    finally:
        for handle in handles:
            if handle:
                if kernel.WaitForSingleObject(handle, 0) != 0: kernel.TerminateProcess(handle, 1)
                kernel.CloseHandle(handle)


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    runner = InterpretationRunner(project_root=tmp_path)
    home = tmp_path / "var/codex-contexts" / ("a" * 64)
    snapshot = baseline()
    calls, bindings = [], []
    result = {"thread_id": "canonical-thread-123", "turn_id": "turn-123", "context_reused": False,
              "context_reset": False, "query_executed": False, "result_state": "definition_interpretation",
              "model": runner.settings["model"], "reasoning_effort": runner.settings["reasoning_effort"],
              "interpretation": validate_interpretation(snapshot, QUESTION, draft(snapshot))}
    state = {"runner": runner, "home": home, "snapshot": snapshot, "calls": calls,
             "bindings": bindings, "result": result, "error": None}
    def exchange(command, data, **kwargs):
        calls.append((command, decode_private(data, limit=MAX_INPUT_BYTES), kwargs))
        checkpoint = ContextCheckpoint(home)
        if checkpoint.resolve() is None: checkpoint.claim_creation()
        checkpoint.bind("canonical-thread-123")
        if state["error"]: raise state["error"]
        return encode_private({"ok": True, "result": state["result"]}, limit=MAX_OUTPUT_BYTES)
    monkeypatch.setattr(module, "_exchange", exchange)
    return state


def run_boundary(state):
    return state["runner"].run(QUESTION, state["snapshot"], context_home=state["home"],
                                on_context_ready=state["bindings"].append)


def test_private_runner_resumes_without_putting_question_or_snapshot_on_command_line(boundary):
    first = run_boundary(boundary)
    boundary["result"]["context_reused"] = True
    second = run_boundary(boundary)
    assert first["thread_id"] == second["thread_id"] == "canonical-thread-123"
    assert boundary["calls"][0][1]["thread_id"] is None
    assert boundary["calls"][1][1]["thread_id"] == first["thread_id"]
    assert all(QUESTION not in " ".join(call[0]) and "contracts" not in " ".join(call[0]) for call in boundary["calls"])
    assert set(boundary["bindings"]) == {first["thread_id"]}
    assert {path.name for path in boundary["home"].iterdir()} == {"agent-context-creation.json", "agent-context-thread.json"}


def test_runner_recovers_identity_on_timeout_and_does_not_retry(boundary):
    boundary["error"] = InterpretationRunError("interpretation_timeout")
    with pytest.raises(InterpretationRunError) as error: run_boundary(boundary)
    assert str(error.value) == "interpretation_timeout"
    assert error.value.thread_id == "canonical-thread-123"
    assert boundary["bindings"] == ["canonical-thread-123"]
    assert len(boundary["calls"]) == 1


@pytest.mark.parametrize("field,value", [
    ("thread_id", "another-thread-123"), ("query_executed", True), ("context_reset", True),
    ("model", "unrequested-model"), ("reasoning_effort", "low"), ("context_reused", "false"),
    ("turn_id", None), ("result_state", "live_success"),
])
def test_runner_rejects_invalid_worker_result_metadata(boundary, field, value):
    boundary["result"][field] = value
    with pytest.raises(InterpretationRunError, match="^interpretation_worker_invalid_response$"):
        run_boundary(boundary)


def test_parent_recomputes_assessment_and_authorization_instead_of_trusting_worker(boundary):
    boundary["result"]["interpretation"].update(execution_authorized=True, publication_authorized=True,
                                               assessment={"private_forged": "not-authoritative"})
    result = run_boundary(boundary)["interpretation"]
    assert not result["execution_authorized"] and not result["publication_authorized"]
    assert "private_forged" not in result["assessment"]


@pytest.mark.parametrize("key", ["request_sha256", "baseline_sha256"])
def test_parent_rejects_stale_or_cross_request_interpretation(boundary, key):
    boundary["result"]["interpretation"][key] = "0" * 64
    with pytest.raises(InterpretationRunError, match="interpretation_worker_invalid_response"):
        run_boundary(boundary)


def test_runner_keeps_detached_snapshot_and_exact_decimal_input(boundary, monkeypatch):
    boundary["snapshot"].contracts["metric:inventory.available"]["parameters"] = {
        "ratio": {"required": False, "default": Decimal("0.12345678901234567890123456789")}}
    frozen = deepcopy(boundary["snapshot"])
    boundary["result"]["interpretation"] = validate_interpretation(frozen, QUESTION, draft(frozen))
    existing = module._exchange
    def mutate(*args, **kwargs):
        boundary["snapshot"].contracts.clear()
        return existing(*args, **kwargs)
    monkeypatch.setattr(module, "_exchange", mutate)
    assert run_boundary(boundary)["interpretation"] == validate_interpretation(frozen, QUESTION, draft(frozen))
    received = boundary["calls"][0][1]["contracts"]["metric:inventory.available"]["parameters"]["ratio"]["default"]
    assert type(received) is Decimal and received == Decimal("0.12345678901234567890123456789")


def test_uncertain_creation_stops_before_any_worker_is_started(boundary):
    ContextCheckpoint(boundary["home"]).claim_creation()
    with pytest.raises(InterpretationRunError, match="context_initialization_uncertain"):
        run_boundary(boundary)
    assert boundary["calls"] == []


@pytest.mark.parametrize("context", [None, "outside", "var/codex-contexts", "var/codex-contexts/user-1", "var/codex-contexts/aaaaaaaaaaaaaaaa/child"])
def test_runner_rejects_invalid_context_before_launch(boundary, context):
    home = boundary["runner"].project_root / context if context else None
    with pytest.raises(InterpretationRunError, match="invalid_interpretation_context"):
        boundary["runner"].run(QUESTION, boundary["snapshot"], context_home=home)
    assert boundary["calls"] == []


def test_unknown_worker_error_does_not_leak_diagnostics(boundary, monkeypatch):
    monkeypatch.setattr(module, "_exchange", lambda *a, **k: encode_private(
        {"ok": False, "error": "PRIVATE_PROVIDER_DETAIL"}, limit=MAX_OUTPUT_BYTES))
    with pytest.raises(InterpretationRunError, match="^interpretation_worker_invalid_response$"):
        run_boundary(boundary)


@pytest.mark.parametrize("validation", ["unsafe_question_text", "PRIVATE_PROVIDER_DETAIL", None, ["invalid_span"]])
def test_only_allowlisted_validation_codes_cross_the_private_worker_boundary(boundary, monkeypatch, validation):
    monkeypatch.setattr(module, "_exchange", lambda *a, **k: encode_private(
        {"ok": False, "error": "interpretation_invalid_response", "validation_code": validation}, limit=MAX_OUTPUT_BYTES))
    with pytest.raises(InterpretationRunError) as error:
        run_boundary(boundary)
    if validation == "unsafe_question_text":
        assert str(error.value) == "interpretation_invalid_response"
        assert error.value.validation_code == validation
    else:
        assert str(error.value) == "interpretation_worker_invalid_response"
        assert error.value.validation_code is None


def test_os_launch_error_is_distinct_from_invalid_worker_response(boundary, monkeypatch):
    def fail(*args, **kwargs): raise OSError("PRIVATE_LOCAL_PATH")
    monkeypatch.setattr(module, "_exchange", fail)
    with pytest.raises(InterpretationRunError, match="^interpretation_worker_failed$"):
        run_boundary(boundary)


@pytest.mark.parametrize("timeout", [0, -1, 901, float("nan"), float("inf"), True, "10"])
def test_invalid_timeouts_rejected(timeout):
    with pytest.raises(ValueError): InterpretationRunner(timeout_seconds=timeout)


def test_worker_passes_only_validated_context_and_exact_snapshot(boundary, monkeypatch):
    run_boundary(boundary)
    request = boundary["calls"][0][1]
    captured = []
    monkeypatch.setattr("app.interpretation_worker.run_codex_interpretation", lambda *a, **k: captured.append((a, k)))
    interpret_request(request, project_root=boundary["runner"].project_root)
    assert captured[0][0][0] == QUESTION and captured[0][1]["context_home"] == boundary["home"]
    request["contracts"]["unknown:key"] = {"id": "key", "version": "1"}
    with pytest.raises(ValueError): interpret_request(request, project_root=boundary["runner"].project_root)
    assert len(captured) == 1
