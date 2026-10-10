"""Ephemeral proposal state bound to a principal, conversation, UI session and request."""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import math
import re
import secrets
import threading
import time

from .errors import RuntimeErrorBase
from .knowledge_changes import _encoded
from .knowledge_proposals import MAX_DECISIONS, ProposalError, assess_proposal, resolve_proposal, snapshot_digest


_BINDING = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TOKEN = re.compile(r"kp_[A-Za-z0-9_-]{32}\Z")


class ProposalContextError(RuntimeErrorBase):
    code = "proposal_unavailable"
    http_status = 404


def _error(code, status):
    messages = {
        "proposal_unavailable": "口径提案不存在或不属于本次会话",
        "proposal_expired": "口径提案已过期或已取消，请重新评估",
        "proposal_conflict": "提案或选择已变化，请恢复最新状态后再试",
        "proposal_baseline_changed": "知识口径已更新，请重新评估本次提案",
        "proposal_invalid": "提案或选择格式无效，未修改任何口径",
        "proposal_capacity": "待评估提案较多，请稍后重试",
    }
    error = ProposalContextError(messages[code])
    error.code, error.http_status = code, status
    return error


@dataclass
class _Entry:
    binding: tuple[str, str, str, str]
    fingerprint: str
    baseline: object
    request: dict | None
    assessment: dict
    expires: float
    size: int
    revision: int = 0
    answer_fingerprint: str | None = None
    cancelled: bool = False
    public_assessment: dict | None = None
    original_assessment: dict | None = None


class ProposalContextStore:
    def __init__(self, *, ttl_seconds=1800, max_bytes=32 * 1024 * 1024,
                 max_owner_entries=8, max_entries=256, clock=time.time, presenter=None):
        if (type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400
                or any(type(value) is not int or value < 1 for value in (max_bytes, max_owner_entries, max_entries))):
            raise ValueError("invalid_proposal_capacity")
        self.ttl = ttl_seconds
        self.max_bytes, self.max_owner_entries, self.max_entries = max_bytes, max_owner_entries, max_entries
        self.clock = clock
        self.presenter = presenter
        self._entries: dict[str, _Entry] = {}
        self._closed: dict[tuple[str, str, str], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _binding(*values):
        if any(not isinstance(value, str) or not _BINDING.fullmatch(value) for value in values):
            raise _error("proposal_invalid", 422)
        return tuple(values)

    def _purge(self, now):
        self._entries = {key: item for key, item in self._entries.items() if item.expires > now}
        self._closed = {key: expiry for key, expiry in self._closed.items() if expiry > now}

    def _get(self, binding, proposal_id, *, allow_cancelled=False):
        entry = self._entries.get(proposal_id) if isinstance(proposal_id, str) and _TOKEN.fullmatch(proposal_id) else None
        if entry is None or entry.binding != binding:
            raise _error("proposal_unavailable", 404)
        if entry.expires <= self.clock() or (entry.cancelled and not allow_cancelled):
            raise _error("proposal_expired", 410)
        return entry

    @staticmethod
    def _response(proposal_id, entry):
        return {"proposal_id": proposal_id, "revision": entry.revision,
                "expires_at": math.floor(entry.expires), "assessment": deepcopy(entry.public_assessment if entry.public_assessment is not None else entry.assessment)}

    def create(self, owner, conversation_id, display_session_id, request_id, baseline, request):
        binding = self._binding(owner, conversation_id, display_session_id, request_id)
        try:
            baseline, request = deepcopy(baseline), deepcopy(request)
            assessment = assess_proposal(baseline, request, include_values=True)
            fingerprint = sha256(_encoded(request).encode()).hexdigest()
            size = len(_encoded([baseline.contracts, request, assessment]).encode()) + 1024
        except ProposalError as error:
            if str(error) == "stale_baseline":
                raise _error("proposal_baseline_changed", 409) from None
            raise _error("proposal_invalid", 422) from None
        except (ValueError, TypeError, RecursionError):
            raise _error("proposal_invalid", 422) from None
        public = self.presenter(baseline, assessment) if self.presenter else None
        size += len(_encoded([assessment, public]).encode())
        with self._lock:
            now = self.clock()
            self._purge(now)
            if binding[:3] in self._closed:
                raise _error("proposal_expired", 410)
            for proposal_id, entry in self._entries.items():
                if entry.binding == binding:
                    if entry.cancelled:
                        raise _error("proposal_expired", 410)
                    if entry.fingerprint != fingerprint:
                        raise _error("proposal_conflict", 409)
                    return self._response(proposal_id, entry)
            if (len(self._entries) >= self.max_entries
                    or sum(item.binding[0] == owner and not item.cancelled for item in self._entries.values()) >= self.max_owner_entries
                    or sum(item.size for item in self._entries.values()) + size > self.max_bytes):
                raise _error("proposal_capacity", 503)
            proposal_id = "kp_" + secrets.token_urlsafe(24)
            entry = _Entry(binding, fingerprint, baseline, request, assessment, now + self.ttl, size)
            entry.public_assessment, entry.original_assessment = public, deepcopy(assessment)
            self._entries[proposal_id] = entry
            return self._response(proposal_id, entry)

    def get(self, owner, conversation_id, display_session_id, request_id, proposal_id, *, current_baseline=None):
        binding = self._binding(owner, conversation_id, display_session_id, request_id)
        with self._lock:
            entry = self._get(binding, proposal_id)
            if current_baseline is not None and snapshot_digest(current_baseline) != entry.request["baseline_sha256"]:
                raise _error("proposal_baseline_changed", 409)
            return self._response(proposal_id, entry)

    def resolve(self, owner, conversation_id, display_session_id, request_id, proposal_id, revision, choices, current_baseline):
        binding = self._binding(owner, conversation_id, display_session_id, request_id)
        if (type(revision) is not int or revision < 0 or not isinstance(choices, dict)
                or len(choices) > MAX_DECISIONS
                or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key)
                       or value not in ("current", "proposed", "defer") for key, value in choices.items())):
            raise _error("proposal_invalid", 422)
        try:
            answer_fingerprint = sha256(_encoded(choices).encode()).hexdigest()
        except (ValueError, TypeError, RecursionError):
            raise _error("proposal_invalid", 422) from None
        with self._lock:
            entry = self._get(binding, proposal_id)
            if snapshot_digest(current_baseline) != entry.request["baseline_sha256"]:
                raise _error("proposal_baseline_changed", 409)
            if revision == entry.revision - 1 and answer_fingerprint == entry.answer_fingerprint:
                return self._response(proposal_id, entry)
            if revision != entry.revision or (entry.revision and not entry.assessment["requires_confirmation"]):
                raise _error("proposal_conflict", 409)
            try:
                report = resolve_proposal(entry.baseline, entry.request, {
                    "proposal_sha256": entry.assessment["proposal_sha256"], "choices": choices,
                }, include_values=True)
            except (ProposalError, ValueError, TypeError, RecursionError):
                raise _error("proposal_invalid", 422) from None
            presentation_report = {**report, "changes": entry.original_assessment["changes"]}
            public = self.presenter(entry.baseline, presentation_report) if self.presenter else None
            size = len(_encoded([entry.baseline.contracts, entry.request, report, entry.original_assessment, public]).encode()) + 1024
            if sum(item.size for item in self._entries.values()) - entry.size + size > self.max_bytes:
                raise _error("proposal_capacity", 503)
            entry.assessment, entry.answer_fingerprint, entry.size = report, answer_fingerprint, size
            entry.public_assessment = public
            entry.revision += 1
            return self._response(proposal_id, entry)

    def cancel(self, owner, conversation_id, display_session_id, request_id, proposal_id):
        binding = self._binding(owner, conversation_id, display_session_id, request_id)
        with self._lock:
            entry = self._get(binding, proposal_id, allow_cancelled=True)
            self._cancel(entry)
            return {"state": "cancelled"}

    @staticmethod
    def _cancel(entry):
        entry.cancelled = True
        entry.baseline, entry.request, entry.assessment, entry.answer_fingerprint = None, None, {}, None
        entry.public_assessment, entry.original_assessment = None, None
        entry.size = 1024

    def close_display_session(self, owner, conversation_id, display_session_id):
        binding = self._binding(owner, conversation_id, display_session_id)
        with self._lock:
            now = self.clock()
            self._purge(now)
            if binding not in self._closed and len(self._closed) >= self.max_entries:
                raise _error("proposal_capacity", 503)
            self._closed.setdefault(binding, now + self.ttl)
            for entry in self._entries.values():
                if entry.binding[:3] == binding:
                    self._cancel(entry)
            return {"state": "closed"}
