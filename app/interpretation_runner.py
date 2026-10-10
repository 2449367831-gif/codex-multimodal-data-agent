"""Bounded hidden subprocess transport for the private Codex interpreter."""

from copy import deepcopy
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import signal
import subprocess
import sys
from threading import Thread
from time import monotonic

from .codex_agent import codex_model_settings, project_codex_home
from .codex_context_checkpoint import ContextCheckpoint, ContextCheckpointError
from .codex_interpretation import InterpretationRunError
from .private_worker_protocol import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, WORKER_ERROR_CODES, VALIDATION_ERROR_CODES, decode_private, encode_private
from .proposal_interpretation import interpretation_input, revalidate_interpretation


def _terminate(process):
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _exchange(command, data, *, cwd, timeout_seconds, cancel_event=None):
    """Drain all pipes under one deadline and cap bytes before JSON decoding."""
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, **kwargs)
    events = Queue()

    def write():
        try:
            process.stdin.write(data)
            process.stdin.close()
            events.put(("input", None))
        except (OSError, ValueError):
            events.put(("error", "interpretation_worker_failed"))

    def read(stream, name, limit):
        total, chunks = 0, []
        try:
            while True:
                block = stream.read1(min(65536, limit - total + 1))
                if not block:
                    break
                total += len(block)
                if total > limit:
                    events.put(("error", "interpretation_output_too_large"))
                    return
                if name == "output":
                    chunks.append(block)
            events.put((name, b"".join(chunks)))
        except (OSError, ValueError):
            events.put(("error", "interpretation_worker_failed"))

    threads = [Thread(target=write, daemon=True),
               Thread(target=read, args=(process.stdout, "output", MAX_OUTPUT_BYTES), daemon=True),
               Thread(target=read, args=(process.stderr, "diagnostics", 65536), daemon=True)]
    deadline = monotonic() + timeout_seconds
    try:
        for thread in threads:
            thread.start()
        completed = {}
        while len(completed) < 3:
            if cancel_event is not None and cancel_event.is_set():
                raise InterpretationRunError("interpretation_cancelled")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise InterpretationRunError("interpretation_timeout")
            try:
                name, value = events.get(timeout=min(remaining, 0.25))
            except Empty:
                continue
            if name == "error":
                raise InterpretationRunError(value)
            completed[name] = value
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise InterpretationRunError("interpretation_cancelled")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise InterpretationRunError("interpretation_timeout")
            try:
                process.wait(timeout=min(remaining, 0.25))
            except subprocess.TimeoutExpired:
                continue
        status = process.returncode
        if status != 0:
            raise InterpretationRunError("interpretation_worker_failed")
        return completed["output"]
    finally:
        _terminate(process)
        for thread in threads:
            thread.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


class InterpretationRunner:
    def __init__(self, *, project_root=None, codex_home=None, timeout_seconds=180,
                 model=None, reasoning_effort=None, model_provider=None, model_base_url=None):
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 900:
            raise ValueError("invalid_interpretation_timeout")
        self.project_root = (Path(project_root) if project_root else Path(__file__).resolve().parents[1]).resolve()
        self.codex_home = project_codex_home(self.project_root, configured_home=codex_home).resolve()
        self.timeout_seconds = timeout_seconds
        self.settings = codex_model_settings(model=model, reasoning_effort=reasoning_effort,
                                             model_provider=model_provider, base_url=model_base_url)

    def run(self, question, snapshot, *, context_home, thread_id=None, on_context_ready=None,
            confirmation=None, cancel_event=None):
        # Validate and detach before spawning: later caller mutations cannot alter
        # either the input or the independent parent-side result verification.
        snapshot = deepcopy(snapshot)
        confirmation = deepcopy(confirmation)
        interpretation_input(snapshot, question, confirmation=confirmation)
        try:
            home = Path(context_home).resolve()
            relative = home.relative_to(self.project_root / "var/codex-contexts")
            if len(relative.parts) != 1 or not re.fullmatch(r"[a-f0-9]{16,64}", relative.name):
                raise ValueError()
        except (ValueError, TypeError):
            raise InterpretationRunError("invalid_interpretation_context") from None
        checkpoint = ContextCheckpoint(home)
        recovered = thread_id
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise InterpretationRunError("interpretation_cancelled")
            recovered = checkpoint.resolve(thread_id)
            if recovered is not None and on_context_ready is not None:
                on_context_ready(recovered)
            message = {"version": 1, "question": question, "contracts": snapshot.contracts,
                       "context_ref": relative.name, "thread_id": recovered,
                       "codex_home": str(self.codex_home), "settings": self.settings, "confirmation": confirmation}
            raw = encode_private(message, limit=MAX_INPUT_BYTES)
            # CREATE_NO_WINDOW keeps python.exe invisible while retaining proper
            # redirected stdio, unlike pythonw's absent sys.stdin/sys.stdout.
            executable = Path(sys.executable)
            if os.name == "nt" and executable.name.lower() == "pythonw.exe":
                executable = executable.with_name("python.exe")
            response = decode_private(_exchange(
                [str(executable), "-m", "app.interpretation_worker", "--project-root", str(self.project_root)],
                raw, cwd=self.project_root, timeout_seconds=self.timeout_seconds, cancel_event=cancel_event,
            ), limit=MAX_OUTPUT_BYTES)
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise ValueError()
            if response["ok"] is False:
                validation_code = response.get("validation_code")
                if (set(response) not in ({"ok", "error"}, {"ok", "error", "validation_code"})
                        or not isinstance(response["error"], str) or response["error"] not in WORKER_ERROR_CODES
                        or ("validation_code" in response and (not isinstance(validation_code, str) or validation_code not in VALIDATION_ERROR_CODES))):
                    raise ValueError()
                raise InterpretationRunError(response["error"], validation_code=validation_code)
            if set(response) != {"ok", "result"}:
                raise ValueError()
            result = response["result"]
            if not isinstance(result, dict) or set(result) != {
                "thread_id", "turn_id", "context_reused", "context_reset", "result_state",
                "interpretation", "model", "reasoning_effort", "query_executed",
            }:
                raise ValueError()
            actual_id = checkpoint.resolve(recovered)
            if actual_id is None or result["thread_id"] != actual_id or result["context_reset"] is not False or result["query_executed"] is not False:
                raise ValueError()
            if (result["model"] != self.settings["model"] or result["reasoning_effort"] != self.settings["reasoning_effort"]
                    or result["result_state"] != "definition_interpretation"
                    or type(result["context_reused"]) is not bool
                    or not isinstance(result["turn_id"], str) or not result["turn_id"]):
                raise ValueError()
            validated = revalidate_interpretation(snapshot, question, result["interpretation"], confirmation=confirmation)
            result["interpretation"] = validated
            return result
        except InterpretationRunError as exc:
            raise InterpretationRunError(str(exc), thread_id=checkpoint.resolve(recovered), turn_id=exc.turn_id,
                                         validation_code=exc.validation_code) from None
        except ContextCheckpointError as exc:
            raise InterpretationRunError(str(exc), thread_id=recovered) from None
        except OSError:
            raise InterpretationRunError("interpretation_worker_failed", thread_id=recovered) from None
        except Exception:
            raise InterpretationRunError("interpretation_worker_invalid_response", thread_id=recovered) from None
        finally:
            try:
                recovered = checkpoint.resolve(recovered)
            except ContextCheckpointError as exc:
                raise InterpretationRunError(str(exc), thread_id=recovered) from None
            if recovered is not None and on_context_ready is not None:
                on_context_ready(recovered)
