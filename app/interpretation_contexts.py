"""Owned, bounded, in-memory lifecycle for natural-language definition requests."""

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import math
import secrets
from threading import Event, RLock
import time

from .errors import RuntimeErrorBase
from .knowledge_changes import _encoded
from .knowledge_proposals import snapshot_digest
from .proposal_contexts import ProposalContextStore
from .proposal_interpretation import interpretation_input, revalidate_interpretation, InterpretationError


class InterpretationContextError(RuntimeErrorBase):
    code = "interpretation_unavailable"
    http_status = 404


def _error(code, status=409):
    messages = {
        "interpretation_unavailable": "本次口径请求不存在或不属于当前会话",
        "interpretation_expired": "本次口径请求已过期，请重新提交",
        "interpretation_conflict": "本次请求或选择已变化，请恢复最新状态",
        "interpretation_invalid": "口径请求或选项无效，未修改任何口径",
        "interpretation_capacity": "待处理口径较多，请稍后重试",
        "interpretation_baseline_changed": "口径基线已更新，本次提案需要重新评估",
        "interpretation_description_required": "选项包含不能直接展示的内部定义，未提交确认或执行取数",
    }
    error = InterpretationContextError(messages[code])
    error.code, error.http_status = code, status
    return error


@dataclass
class _Entry:
    binding: tuple
    fingerprint: str
    baseline: object
    question: str
    expires: float
    token: str
    state: str = "running"
    revision: int = 0
    size: int = 0
    confirmation: list | None = None
    private_questions: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    choices: dict = field(default_factory=dict)
    proposal: dict | None = None
    cancel_event: Event = field(default_factory=Event)
    last_selection: tuple | None = None
    error: str | None = None


class InterpretationContextStore:
    def __init__(self, *, screen_text, create_proposal, clock=time.time, ttl_seconds=1800,
                 max_bytes=32 * 1024 * 1024, max_entries=256, max_owner_entries=8):
        if any(type(value) is not int or value < 1 for value in (ttl_seconds, max_bytes, max_entries, max_owner_entries)):
            raise ValueError("invalid_interpretation_capacity")
        self.screen_text, self.create_proposal, self.clock = screen_text, create_proposal, clock
        self.ttl, self.max_bytes = ttl_seconds, max_bytes
        self.max_entries, self.max_owner_entries = max_entries, max_owner_entries
        self._entries, self._closed = {}, {}
        self._lock = RLock()

    @staticmethod
    def _binding(binding):
        if not isinstance(binding, tuple) or len(binding) != 4:
            raise _error("interpretation_invalid", 422)
        try:
            return ProposalContextStore._binding(*binding)
        except RuntimeErrorBase:
            raise _error("interpretation_invalid", 422) from None

    def _get(self, binding, identifier):
        self._binding(binding)
        entry = self._entries.get(identifier) if isinstance(identifier, str) else None
        if entry is None or entry.binding != binding:
            raise _error("interpretation_unavailable", 404)
        if entry.expires <= self.clock():
            entry.cancel_event.set()
            raise _error("interpretation_expired", 410)
        return entry

    @staticmethod
    def _size(entry):
        return len(_encoded([entry.baseline.contracts if entry.baseline else None, entry.question,
                             entry.confirmation, entry.private_questions, entry.questions,
                             entry.choices, entry.proposal]).encode()) + 2048

    def _capacity(self, entry, old_size=0):
        size = self._size(entry)
        if sum(item.size for item in self._entries.values()) - old_size + size > self.max_bytes:
            raise _error("interpretation_capacity", 503)
        entry.size = size

    @staticmethod
    def _response(identifier, entry):
        return deepcopy({"interpretation_id": identifier, "revision": entry.revision,
                         "expires_at": math.floor(entry.expires), "state": entry.state,
                         "questions": entry.questions, "choices": entry.choices,
                         "proposal": entry.proposal, "error": entry.error,
                         "result_state": "definition_interpretation",
                         "process": {"state": "not_queried", "steps": []}})

    def create(self, binding, baseline, question):
        self._binding(binding)
        baseline = deepcopy(baseline)
        try:
            payload = interpretation_input(baseline, question)
        except InterpretationError:
            raise _error("interpretation_invalid", 422) from None
        fingerprint = sha256(_encoded([payload["request_sha256"], payload["baseline_sha256"]]).encode()).hexdigest()
        with self._lock:
            now = self.clock()
            for entry in self._entries.values():
                if entry.expires <= now: entry.cancel_event.set()
            self._entries = {key: entry for key, entry in self._entries.items() if entry.expires > now}
            self._closed = {key: expiry for key, expiry in self._closed.items() if expiry > now}
            if binding[:3] in self._closed:
                raise _error("interpretation_expired", 410)
            for identifier, entry in self._entries.items():
                if entry.binding == binding:
                    if entry.fingerprint != fingerprint:
                        raise _error("interpretation_conflict")
                    return self._response(identifier, entry), None
            if (len(self._entries) >= self.max_entries or
                    sum(entry.binding[0] == binding[0] and entry.state != "cancelled" for entry in self._entries.values()) >= self.max_owner_entries):
                raise _error("interpretation_capacity", 503)
            identifier, token = "ki_" + secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            entry = _Entry(binding, fingerprint, baseline, question, now + self.ttl, token)
            self._capacity(entry)
            self._entries[identifier] = entry
            return self._response(identifier, entry), token

    def get(self, binding, identifier, *, current_baseline=None):
        with self._lock:
            entry = self._get(binding, identifier)
            if current_baseline is not None and entry.baseline is not None:
                self._baseline(entry, current_baseline)
            return self._response(identifier, entry)

    @staticmethod
    def _baseline(entry, current):
        if snapshot_digest(entry.baseline) != snapshot_digest(current):
            raise _error("interpretation_baseline_changed")

    def job(self, binding, identifier, token):
        with self._lock:
            entry = self._get(binding, identifier)
            if entry.token != token or entry.state != "running" or entry.cancel_event.is_set():
                return None
            return {"question": entry.question, "snapshot": deepcopy(entry.baseline),
                    "confirmation": deepcopy(entry.confirmation), "cancel_event": entry.cancel_event}

    def finish(self, binding, identifier, token, result, current_baseline):
        with self._lock:
            entry = self._get(binding, identifier)
            if entry.token != token or entry.state != "running" or entry.cancel_event.is_set():
                return None
            self._baseline(entry, current_baseline)
            try:
                if result.get("query_executed") is not False: raise InterpretationError("unexpected_query")
                interpreted = revalidate_interpretation(entry.baseline, entry.question, result["interpretation"],
                                                       confirmation=entry.confirmation)
            except (InterpretationError, TypeError, KeyError, AttributeError):
                raise _error("interpretation_invalid", 422) from None
            public = []
            for index, question in enumerate(interpreted["questions"]):
                prompt = self.screen_text(question["prompt"])
                labels = [self.screen_text(option["label"]) for option in question["options"]]
                if prompt is None or any(label is None for label in labels):
                    raise _error("interpretation_description_required", 422)
                public.append({"id": f"q{index}", "prompt": prompt, "selection": "single",
                               "options": [{"id": f"o{i}", "label": label} for i, label in enumerate(labels)
                                           if question["options"][i]["id"] != "defer" and label != "暂不确认"] +
                                          [{"id": "defer", "label": "暂不确认"}]})
            # Keep only a reference to review state: its choices/revision may
            # subsequently change through the existing proposal endpoints.
            proposal = {"proposal_id": "x" * 128} if interpreted["review_ready"] else None
            updated = _Entry(**{**entry.__dict__, "private_questions": interpreted["questions"],
                              "questions": public, "choices": {}, "proposal": proposal,
                              "revision": entry.revision + 1, "error": None,
                              "state": "needs_confirmation" if public else "review_ready" if proposal else "no_change"})
            self._capacity(updated, entry.size)
            if interpreted["review_ready"]:
                created = self.create_proposal(*binding, entry.baseline, interpreted["proposal"])
                if not isinstance(created.get("proposal_id"), str) or not 1 <= len(created["proposal_id"]) <= 128:
                    raise _error("interpretation_invalid", 422)
                updated.proposal = {"proposal_id": created["proposal_id"]}
                updated.size = self._size(updated)
            self._entries[identifier] = updated
            return self._response(identifier, updated)

    def select(self, binding, identifier, revision, choices, current_baseline):
        with self._lock:
            entry = self._get(binding, identifier)
            if entry.state == "cancelled":
                raise _error("interpretation_conflict")
            self._baseline(entry, current_baseline)
            if type(revision) is not int or not isinstance(choices, dict) or any(not isinstance(v, str) for v in choices.values()):
                raise _error("interpretation_invalid", 422)
            fingerprint = sha256(_encoded(choices).encode()).hexdigest()
            if entry.last_selection == (revision, fingerprint):
                return self._response(identifier, entry), None
            if revision != entry.revision or entry.state not in {"needs_confirmation", "deferred"}:
                raise _error("interpretation_conflict")
            if set(choices) != {item["id"] for item in entry.questions}:
                raise _error("interpretation_invalid", 422)
            if any(choices[item["id"]] not in {option["id"] for option in item["options"]} for item in entry.questions):
                raise _error("interpretation_invalid", 422)
            confirmation = deepcopy(entry.confirmation or [])
            defer = "defer" in choices.values()
            if not defer:
                for index, question in enumerate(entry.private_questions):
                    selected = int(choices[f"q{index}"][1:])
                    confirmation.append({"question": deepcopy(question), "selected_option": question["options"][selected]["id"]})
                try:
                    interpretation_input(entry.baseline, entry.question, confirmation=confirmation)
                except InterpretationError:
                    raise _error("interpretation_invalid", 422) from None
            token = secrets.token_urlsafe(24)
            updated = _Entry(**{**entry.__dict__, "state": "deferred" if defer else "running",
                              "revision": entry.revision + 1, "choices": dict(choices),
                              "confirmation": entry.confirmation if defer else confirmation,
                              "token": token, "cancel_event": Event(), "last_selection": (revision, fingerprint)})
            self._capacity(updated, entry.size)
            self._entries[identifier] = updated
            return self._response(identifier, updated), None if defer else token

    def fail(self, binding, identifier, token, code):
        messages = {
            "interpretation_timeout": "口径解析等待超时，未执行取数或修改口径",
            "interpretation_cancelled": "本次口径解析已停止，未执行取数或修改口径",
            "conversation_busy": "同一机器还有请求正在处理，请稍后重新提交",
            "interpretation_baseline_changed": "口径基线已更新，请重新评估本次提案",
            "interpretation_description_required": "口径说明包含不能直接展示的内部定义，未提交确认",
            "proposal_description_required": "提案包含尚需核对的复杂定义，未提交确认或发布",
        }
        with self._lock:
            try: entry = self._get(binding, identifier)
            except InterpretationContextError: return
            if entry.token != token or entry.state != "running": return
            entry.state, entry.error = "failed", messages.get(code, "本次口径解析未完成，未执行取数或修改口径")
            entry.revision += 1

    def cancel(self, binding, identifier):
        with self._lock:
            entry = self._get(binding, identifier)
            entry.cancel_event.set()
            entry.state = "cancelled"
            entry.baseline, entry.confirmation, entry.proposal = None, None, None
            entry.private_questions, entry.questions, entry.choices = [], [], {}
            entry.question, entry.error = "", None
            entry.size = self._size(entry)
            return self._response(identifier, entry)

    def close_display_session(self, binding):
        ProposalContextStore._binding(*binding)
        if len(binding) != 3: raise _error("interpretation_invalid", 422)
        with self._lock:
            now = self.clock()
            self._closed = {key: expiry for key, expiry in self._closed.items() if expiry > now}
            if binding not in self._closed and len(self._closed) >= self.max_entries:
                raise _error("interpretation_capacity", 503)
            self._closed[binding] = now + self.ttl
            for identifier, entry in self._entries.items():
                if entry.binding[:3] == binding and entry.expires > now:
                    self.cancel(entry.binding, identifier)
            return {"state": "closed"}
