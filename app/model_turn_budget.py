"""Bound SDK turns without overlapping retries in a canonical conversation."""

import os
from threading import Event, Thread

from .model_turn_stream import TurnBudget, collect_turn


_UNCERTAIN_THREADS = []


def attempt_budget(attempt):
    # A query-plan turn should either return a structured contract promptly or
    # fail into the explicit retry/error state. Long xhigh hangs used to hold a
    # A model-first request commonly needs a planning turn, a Doris turn and a
    # result turn. The old 90/45 second caps interrupted valid plans before the
    # host could publish their verified table. Keep the wait bounded, but give a
    # normal first turn enough room for structured planning. Deployments can
    # still tune these values through env.
    key, default, ceiling = (
        ("AGENT_MODEL_TURN_TIMEOUT_SECONDS", 180, 300)
        if attempt == 0
        else ("AGENT_MODEL_RETRY_TIMEOUT_SECONDS", 90, 180)
    )
    try:
        value = int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default
    return max(5, min(value, ceiling))


class ModelTurnCancellationUnconfirmed(RuntimeError):
    """The enclosing worker must exit; starting another turn is unsafe."""


class ModelTurnBudgetExceeded(TimeoutError):
    """A timed-out turn has been confirmed stopped and can be retried."""


def run_turn(thread, prompt, *, effort, model, sandbox,
             timeout_seconds=75.0, cancel_grace_seconds=5.0,
             compaction_allowance_seconds=90.0):
    if timeout_seconds <= 0 or cancel_grace_seconds <= 0 or not 0 <= compaction_allowance_seconds <= 90:
        raise ValueError("model turn budgets must be positive")
    if any(value is thread for value in _UNCERTAIN_THREADS):
        raise ModelTurnCancellationUnconfirmed("model_turn_cancellation_unconfirmed")
    # Older adapters expose only run(). The installed production SDK exposes
    # turn(), whose public handle supports exact-turn cancellation.
    if not callable(getattr(type(thread), "turn", None)):
        return thread.run(prompt, effort=effort, model=model, sandbox=sandbox)
    handle = thread.turn(prompt, effort=effort, model=model, sandbox=sandbox)
    budget = TurnBudget(timeout_seconds, compaction_allowance_seconds)
    done = Event()
    outcome = {}

    def collect():
        try:
            outcome["result"] = (collect_turn(handle, budget)
                                 if callable(getattr(type(handle), "stream", None)) else handle.run())
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            done.set()

    reader = Thread(target=collect, name="model-turn-collector", daemon=True)
    reader.start()
    while not done.is_set():
        remaining = budget.remaining()
        if remaining <= 0:
            break
        done.wait(min(remaining, 0.1))
    if done.is_set():
        reader.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    cancel_done = Event()
    def cancel():
        try:
            handle.interrupt()
        except Exception:
            pass  # Only the terminal event confirms cancellation, not this RPC.
        finally:
            cancel_done.set()

    interrupter = Thread(target=cancel, name="model-turn-interrupter", daemon=True)
    interrupter.start()
    terminal = done.wait(cancel_grace_seconds)
    acknowledged = cancel_done.wait(cancel_grace_seconds)
    if not terminal or not acknowledged:
        _UNCERTAIN_THREADS.append(thread)
        raise ModelTurnCancellationUnconfirmed("model_turn_cancellation_unconfirmed")
    reader.join()
    interrupter.join()
    result = outcome.get("result")
    status = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", None)))
    if status == "completed":
        return result  # Completion won the cancellation race; do not discard it.
    if status != "interrupted":
        _UNCERTAIN_THREADS.append(thread)
        raise ModelTurnCancellationUnconfirmed("model_turn_cancellation_unconfirmed")
    raise ModelTurnBudgetExceeded("model_turn_budget_exhausted")
