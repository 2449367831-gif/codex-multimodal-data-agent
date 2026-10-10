"""Observe public SDK events without exposing model or query content."""

from threading import Lock
from time import monotonic

from .worker_progress import emit


class TurnBudget:
    """Credit only observed compaction time, with a hard total allowance."""

    def __init__(self, planning_seconds, compaction_seconds, *, clock=monotonic):
        self.clock = clock
        self.started = clock()
        self.planning_seconds = planning_seconds
        self.compaction_seconds = compaction_seconds
        self.compacted_seconds = 0.0
        self.active = None
        self.seen = set()
        self.lock = Lock()

    def compaction_started(self, item_id):
        with self.lock:
            if self.active is not None or item_id in self.seen:
                return
            self.seen.add(item_id)
            self.active = (item_id, self.clock())
        emit("compaction", "started")

    def compaction_completed(self, item_id):
        with self.lock:
            if self.active is None or self.active[0] != item_id:
                return
            self.compacted_seconds += max(0, self.clock() - self.active[1])
            self.active = None
        emit("compaction", "completed")

    def remaining(self):
        with self.lock:
            now = self.clock()
            active_seconds = max(0, now - self.active[1]) if self.active else 0.0
            credit = min(self.compaction_seconds, self.compacted_seconds + active_seconds)
            return max(0.0, self.planning_seconds + credit - (now - self.started))


def collect_turn(handle, budget):
    """Collect the public stream, retaining the SDK's final-message semantics."""
    from openai_codex import TurnResult
    from openai_codex.generated.v2_all import (
        AgentMessageThreadItem, ContextCompactionThreadItem, ItemCompletedNotification,
        ItemStartedNotification, MessagePhase, ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification, TurnStatus,
    )

    items, usage, completed = [], None, None
    final_response, unknown_phase_response, last_agent_response = None, None, None
    stream = handle.stream()
    try:
        for event in stream:
            payload = event.payload
            if getattr(payload, "thread_id", None) != handle.thread_id:
                continue
            if isinstance(payload, TurnCompletedNotification):
                if payload.turn.id == handle.id:
                    completed = payload.turn
                    break
                continue
            if getattr(payload, "turn_id", None) != handle.id:
                continue
            if isinstance(payload, (ItemStartedNotification, ItemCompletedNotification)):
                item = payload.item.root
                if isinstance(item, ContextCompactionThreadItem):
                    if isinstance(payload, ItemStartedNotification):
                        budget.compaction_started(item.id)
                    else:
                        budget.compaction_completed(item.id)
                if isinstance(payload, ItemCompletedNotification):
                    items.append(payload.item)
                    if isinstance(item, AgentMessageThreadItem):
                        # Some gateways label a valid structured contract as commentary
                        # even though it is the last model message. Keep it as a private
                        # parser fallback; callers still validate the contract before any
                        # database execution or user-visible response.
                        last_agent_response = item.text
                        if item.phase == MessagePhase.final_answer:
                            final_response = item.text
                        elif item.phase is None:
                            unknown_phase_response = item.text
            elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
                usage = payload.token_usage
    finally:
        stream.close()
    if completed is None:
        raise RuntimeError("turn completed event not received")
    if completed.status == TurnStatus.failed:
        message = completed.error.message if completed.error else "model_turn_failed"
        raise RuntimeError(message)
    return TurnResult(
        id=completed.id, status=completed.status, error=completed.error,
        started_at=completed.started_at, completed_at=completed.completed_at,
        duration_ms=completed.duration_ms, final_response=(
            final_response
            if final_response is not None
            else unknown_phase_response
            if unknown_phase_response is not None
            else last_agent_response
        ), items=items, usage=usage,
    )
