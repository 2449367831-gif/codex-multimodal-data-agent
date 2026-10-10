"""Bounded, data-free child progress used only for operational diagnostics."""

from contextlib import contextmanager
import json
import os
import sys
import time


PREFIX = "AGENT_WORKER_PROGRESS "
PHASES = frozenset({"knowledge", "model", "compaction", "query", "validation"})
EVENTS = frozenset({"started", "completed", "failed", "timeout"})
_STARTED = time.monotonic()


def _valid(value):
    return (isinstance(value, dict) and set(value) == {"phase", "event", "step", "elapsed_ms"}
            and isinstance(value["phase"], str) and value["phase"] in PHASES
            and isinstance(value["event"], str) and value["event"] in EVENTS
            and type(value["step"]) is int and 0 <= value["step"] <= 128
            and type(value["elapsed_ms"]) is int and 0 <= value["elapsed_ms"] <= 3_600_000)


def emit(phase, event, step=0):
    if os.getenv("AGENT_CHAT_INTERNAL_RESULT_STDOUT") != "1":
        return
    record = {"phase": phase, "event": event, "step": step,
              "elapsed_ms": max(0, int((time.monotonic()-_STARTED)*1000))}
    if not _valid(record):
        return
    try:
        print(PREFIX + json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


@contextmanager
def phase(name, *, step=0):
    emit(name, "started", step)
    try:
        yield
    except BaseException:
        emit(name, "failed", step)
        raise
    else:
        emit(name, "completed", step)


def last_progress(stderr):
    """Never forward child messages, SQL, extra keys, or malformed fields."""
    if isinstance(stderr, bytes):
        stderr = stderr[-65536:].decode("utf-8", errors="replace")
    if not isinstance(stderr, str):
        return None
    for line in reversed(stderr[-65536:].splitlines()):
        if not line.startswith(PREFIX) or len(line) > 512:
            continue
        try:
            value = json.loads(line[len(PREFIX):])
        except (ValueError, TypeError):
            continue
        if _valid(value):
            return value
    return None
