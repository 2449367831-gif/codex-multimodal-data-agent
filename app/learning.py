"""Private learning inbox for query tests and reviewed knowledge candidates.

The ledger deliberately sits beside the chat session registry instead of inside
the Codex transcript. It records bounded user questions and lifecycle outcomes,
but never stores SQL, result rows, credentials, or model transcripts. Every
candidate produced from the ledger is evidence for review; it is not a runtime
business contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import unicodedata
from typing import Any, Iterator, Mapping

from .errors import RuntimeErrorBase


LEARNING_SCHEMA_VERSION = "1.0.0"
DEFAULT_MAX_TEXT_LENGTH = 4000
DEFAULT_MAX_NOTE_LENGTH = 1200
MAX_PRIVATE_EXAMPLES = 5
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,200}$")
CONVERSATION_ID_PATTERN = re.compile(r"^c_[A-Za-z0-9_-]{10,120}$")
PRINCIPAL_HASH_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,200}$")

QUERY_OUTCOMES = frozenset(
    {"completed", "failed", "blocked", "clarification_required", "running"}
)
FEEDBACK_TYPES = frozenset({"accepted", "correction"})
ISSUE_TYPES = frozenset(
    {
        "definition",
        "date_window",
        "scope",
        "unit_grain",
        "missing_result",
        "execution_failure",
        "other",
    }
)

ROUTE_LABELS = {
    "inbound": "入库",
    "sales_outbound": "销售出库",
    "inventory_turnover": "库存周转",
    "inventory": "库存",
    "dms_points_redemption": "DMS 积分兑换",
    "procurement": "采购与在途",
    "other": "其他",
}
ISSUE_LABELS = {
    "definition": "业务口径",
    "date_window": "日期或时间窗口",
    "scope": "范围（仓库、SKU 等）",
    "unit_grain": "单位或粒度",
    "missing_result": "结果缺失",
    "execution_failure": "执行失败",
    "other": "其他",
}

_PROCESS_LOCK = threading.RLock()
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DATE_RE = re.compile(
    r"(?:\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*日?)"
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_SQL_START_RE = re.compile(r"^\s*(?:select|with|insert|update|delete|drop|alter|create)\b", re.I)
_SQL_FRAGMENT_RE = re.compile(
    r"(?:\bselect\b[\s\S]{0,2000}\bfrom\b|\bwith\b[\s\S]{0,2000}\bselect\b|"
    r"\b(?:insert|update|delete|drop|alter|create)\b[\s\S]{0,2000};)",
    re.I,
)
_SECRET_RE = re.compile(
    r"(?ix)(?:"
    r"sk-[a-z0-9_-]{16,}|"
    r"bearer\s+[a-z0-9._~+/=-]{12,}|"
    r"(?:password|passwd|secret|token|api[_ -]?key|authorization)\s*[:=]\s*[^\s,;]+"
    r")"
)
_URL_RE = re.compile(r"https?://[^\s]+", re.I)


class LearningError(RuntimeErrorBase):
    code = "learning_error"
    http_status = 500


class LearningFeedbackValidationError(LearningError):
    code = "learning_feedback_invalid"
    http_status = 422


class LearningFeedbackNotFoundError(LearningError):
    code = "learning_feedback_not_found"
    http_status = 404


class LearningOwnershipError(LearningError):
    code = "learning_feedback_not_found"
    http_status = 404


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: Any, *, maximum: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    text = unicodedata.normalize("NFKC", value)
    text = _CONTROL_RE.sub(" ", text)
    text = _SECRET_RE.sub("[REDACTED]", text)
    text = _URL_RE.sub("[URL]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > maximum:
        text = text[:maximum].rstrip()
    return text


def sanitize_question(value: Any) -> str:
    """Keep a useful test phrase while removing obvious sensitive payloads."""

    text = _safe_text(value, maximum=DEFAULT_MAX_TEXT_LENGTH)
    if not text:
        return "口径选择提交"
    # A pasted SQL statement is not useful as a learning paraphrase and must not
    # enter the private digest verbatim.
    if _SQL_START_RE.match(text) or _SQL_FRAGMENT_RE.search(text):
        return "用户提交了受保护的查询文本"
    return text


def sanitize_private_note(value: Any) -> str:
    """Keep review context useful without persisting pasted query text."""

    text = _safe_text(value, maximum=DEFAULT_MAX_NOTE_LENGTH)
    if not text:
        return ""
    if _SQL_START_RE.match(text) or _SQL_FRAGMENT_RE.search(text):
        return "用户提交了受保护的查询文本"
    return text


def normalize_question(value: Any) -> str:
    """Create a stable, non-semantic hash key for repeated question patterns."""

    text = sanitize_question(value).casefold()
    text = _DATE_RE.sub(" <date> ", text)
    text = _NUMBER_RE.sub(" <number> ", text)
    replacements = (
        ("昨日", "昨天"),
        ("数量", "量"),
        ("库存量", "库存"),
        ("销售发货", "销售出库"),
        ("到货量", "入库"),
        ("收货量", "入库"),
        ("入库量", "入库"),
        ("到货", "入库"),
        ("收货", "入库"),
        ("出库量", "出库"),
        ("商品", "sku"),
        ("货品", "sku"),
        ("仓库数量", "仓库数量"),
        ("吨数", "吨"),
        ("重量", "吨"),
        ("查询一下", ""),
        ("查询", ""),
        ("统计一下", ""),
        ("统计", ""),
        ("请问", ""),
        ("帮我看下", ""),
        ("帮我看", ""),
    )
    for source, target in replacements:
        text = text.replace(source, target)
    text = re.sub(r"[^a-z0-9\u3400-\u9fff<>]+", "", text)
    return text or "unclassified"


def question_hash(value: Any) -> str:
    return hashlib.sha256(normalize_question(value).encode("utf-8")).hexdigest()


def classify_route(value: Any) -> str:
    """Classify a question for aggregation only; this is not metric routing."""

    text = sanitize_question(value).casefold()
    if "dms" in text and any(term in text for term in ("积分兑换", "积分订单", "积分")):
        return "dms_points_redemption"
    if any(term in text for term in ("入库", "到货", "收货", "进仓")):
        return "inbound"
    if "周转" in text and "库存" in text:
        return "inventory_turnover"
    if "出库" in text and any(term in text for term in ("销售", "实际")):
        return "sales_outbound"
    if any(term in text for term in ("采购", "在途")):
        return "procurement"
    if "库存" in text:
        return "inventory"
    return "other"


def _require_principal(value: Any) -> str:
    principal = str(value or "").strip()
    if not principal or not PRINCIPAL_HASH_PATTERN.fullmatch(principal):
        raise LearningFeedbackValidationError("学习记录身份无效")
    return principal


def _require_conversation(value: Any) -> str:
    conversation = str(value or "").strip()
    if not CONVERSATION_ID_PATTERN.fullmatch(conversation):
        raise LearningFeedbackValidationError("学习记录会话无效")
    return conversation


def _require_request(value: Any) -> str:
    request_id = str(value or "").strip()
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise LearningFeedbackValidationError("学习记录请求无效")
    return request_id


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class LearningLedger:
    """SQLite-backed, principal-scoped learning inbox.

    The database may be the same file as ``ChatSessionStore``. Separate table
    names and a process lock keep the upgrade non-destructive while allowing the
    chat API to record an event without placing user text in the session tables.
    """

    _events_table = "chat_learning_events_v1"
    _feedback_table = "chat_query_feedback_v1"
    _definitions_table = "chat_learning_definitions_v1"
    _candidates_table = "chat_learning_candidates_v1"

    def __init__(self, path: Path, *, digest_root: Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.digest_root = Path(
            digest_root or self.path.parent / "learning-digests"
        ).expanduser().resolve()
        self.digest_root.mkdir(parents=True, exist_ok=True)
        self._lock = _PROCESS_LOCK
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=10.0,
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {self._events_table} (
                    event_id TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    normalized_question TEXT NOT NULL,
                    question_hash TEXT NOT NULL,
                    route_family TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    execution_status TEXT NOT NULL,
                    has_business_result INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    UNIQUE (principal_hash, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_learning_events_owner
                    ON {self._events_table} (principal_hash, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_learning_events_pattern
                    ON {self._events_table} (principal_hash, question_hash, created_at DESC);

                CREATE TABLE IF NOT EXISTS {self._feedback_table} (
                    feedback_id TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    issue_type TEXT,
                    note TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE (principal_hash, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_learning_feedback_owner
                    ON {self._feedback_table} (principal_hash, created_at DESC);

                CREATE TABLE IF NOT EXISTS {self._definitions_table} (
                    definition_id TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    conversation_id TEXT,
                    metric_name TEXT NOT NULL,
                    definition_text TEXT NOT NULL,
                    example_question TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_definitions_owner
                    ON {self._definitions_table} (principal_hash, created_at DESC);

                CREATE TABLE IF NOT EXISTS {self._candidates_table} (
                    candidate_id TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    candidate_type TEXT NOT NULL,
                    candidate_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'needs_review',
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    UNIQUE (principal_hash, candidate_type, candidate_key)
                );
                CREATE INDEX IF NOT EXISTS idx_learning_candidates_owner
                    ON {self._candidates_table}
                        (principal_hash, status, last_seen_at DESC);
                """
            )

    @staticmethod
    def _candidate_id() -> str:
        return "lc_" + secrets.token_urlsafe(18)

    def _upsert_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        principal_hash: str,
        candidate_type: str,
        candidate_key: str,
        payload: Mapping[str, Any],
        now: float,
        increment: bool = True,
    ) -> None:
        existing = connection.execute(
            f"""
            SELECT candidate_id, occurrence_count, status
            FROM {self._candidates_table}
            WHERE principal_hash = ? AND candidate_type = ? AND candidate_key = ?
            """,
            (principal_hash, candidate_type, candidate_key),
        ).fetchone()
        if existing is None:
            connection.execute(
                f"""
                INSERT INTO {self._candidates_table}
                    (candidate_id, principal_hash, candidate_type, candidate_key,
                     payload_json, occurrence_count, status, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, 1, 'needs_review', ?, ?)
                """,
                (
                    self._candidate_id(),
                    principal_hash,
                    candidate_type,
                    candidate_key,
                    _json(dict(payload)),
                    now,
                    now,
                ),
            )
            return
        occurrence_count = int(existing["occurrence_count"])
        if increment:
            occurrence_count += 1
        connection.execute(
            f"""
            UPDATE {self._candidates_table}
            SET payload_json = ?, occurrence_count = ?, last_seen_at = ?,
                status = CASE WHEN status = 'rejected' THEN 'needs_review' ELSE status END
            WHERE candidate_id = ? AND principal_hash = ?
            """,
            (
                _json(dict(payload)),
                occurrence_count,
                now,
                str(existing["candidate_id"]),
                principal_hash,
            ),
        )

    def _remove_candidate_occurrence(
        self,
        connection: sqlite3.Connection,
        *,
        principal_hash: str,
        candidate_type: str,
        candidate_key: str,
    ) -> None:
        """Remove evidence withdrawn by an edited feedback record."""

        existing = connection.execute(
            f"""
            SELECT candidate_id, occurrence_count
            FROM {self._candidates_table}
            WHERE principal_hash = ? AND candidate_type = ? AND candidate_key = ?
            """,
            (principal_hash, candidate_type, candidate_key),
        ).fetchone()
        if existing is None:
            return
        if int(existing["occurrence_count"]) <= 1:
            connection.execute(
                f"""
                DELETE FROM {self._candidates_table}
                WHERE candidate_id = ? AND principal_hash = ?
                """,
                (str(existing["candidate_id"]), principal_hash),
            )
            return
        connection.execute(
            f"""
            UPDATE {self._candidates_table}
            SET occurrence_count = occurrence_count - 1
            WHERE candidate_id = ? AND principal_hash = ?
            """,
            (str(existing["candidate_id"]), principal_hash),
        )

    def record_query(
        self,
        *,
        principal_hash: str,
        conversation_id: str,
        request_id: str,
        question: str,
        outcome: str,
        execution_status: str,
        has_business_result: bool,
        route_family: str | None = None,
        created_at: float | None = None,
    ) -> str:
        principal = _require_principal(principal_hash)
        conversation = _require_conversation(conversation_id)
        request = _require_request(request_id)
        safe_question = sanitize_question(question)
        normalized = normalize_question(safe_question)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        safe_outcome = str(outcome or "").strip().casefold()
        if safe_outcome not in QUERY_OUTCOMES:
            safe_outcome = "blocked"
        safe_execution = _safe_text(
            execution_status or "unknown", maximum=80, fallback="unknown"
        )
        route = route_family if route_family in ROUTE_LABELS else classify_route(safe_question)
        now = float(created_at if created_at is not None else time.time())
        with self._connection() as connection:
            existing = connection.execute(
                f"""
                SELECT event_id, question_hash
                FROM {self._events_table}
                WHERE principal_hash = ? AND request_id = ?
                """,
                (principal, request),
            ).fetchone()
            event_id = str(existing["event_id"]) if existing else "le_" + secrets.token_urlsafe(18)
            if existing is None:
                connection.execute(
                    f"""
                    INSERT INTO {self._events_table}
                        (event_id, principal_hash, conversation_id, request_id,
                         question_text, normalized_question, question_hash, route_family,
                         outcome, execution_status, has_business_result, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        principal,
                        conversation,
                        request,
                        safe_question,
                        normalized,
                        digest,
                        route,
                        safe_outcome,
                        safe_execution,
                        int(bool(has_business_result)),
                        now,
                    ),
                )
            else:
                connection.execute(
                    f"""
                    UPDATE {self._events_table}
                    SET conversation_id = ?, question_text = ?, normalized_question = ?,
                        question_hash = ?, route_family = ?, outcome = ?,
                        execution_status = ?, has_business_result = ?, created_at = ?
                    WHERE event_id = ? AND principal_hash = ?
                    """,
                    (
                        conversation,
                        safe_question,
                        normalized,
                        digest,
                        route,
                        safe_outcome,
                        safe_execution,
                        int(bool(has_business_result)),
                        now,
                        event_id,
                        principal,
                    ),
                )
            old_question_hash = str(existing["question_hash"]) if existing else ""
            if old_question_hash and old_question_hash != digest:
                self._remove_candidate_occurrence(
                    connection,
                    principal_hash=principal,
                    candidate_type="question_pattern",
                    candidate_key=old_question_hash,
                )
            self._upsert_candidate(
                connection,
                principal_hash=principal,
                candidate_type="question_pattern",
                candidate_key=digest,
                payload={
                    "normalized_question": normalized,
                    "example": safe_question,
                    "route_family": route,
                },
                now=now,
                increment=existing is None or old_question_hash != digest,
            )
        self.build_digest(principal)
        return event_id

    def record_definition_submission(
        self,
        *,
        principal_hash: str,
        conversation_id: str | None,
        metric_name: str,
        definition_text: str,
        example_question: str | None = None,
        created_at: float | None = None,
    ) -> str:
        principal = _require_principal(principal_hash)
        conversation = None
        if conversation_id:
            conversation = _require_conversation(conversation_id)
        name = _safe_text(metric_name, maximum=200)
        definition = sanitize_private_note(definition_text)
        example = sanitize_question(example_question) if example_question else None
        if not name or not definition:
            raise LearningFeedbackValidationError("口径候选内容无效")
        now = float(created_at if created_at is not None else time.time())
        definition_id = "ld_" + secrets.token_urlsafe(18)
        with self._connection() as connection:
            connection.execute(
                f"""
                INSERT INTO {self._definitions_table}
                    (definition_id, principal_hash, conversation_id, metric_name,
                     definition_text, example_question, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (definition_id, principal, conversation, name, definition, example, now),
            )
        self.build_digest(principal)
        return definition_id

    def record_feedback(
        self,
        *,
        principal_hash: str,
        conversation_id: str,
        request_id: str,
        feedback_type: str,
        issue_type: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        principal = _require_principal(principal_hash)
        conversation = _require_conversation(conversation_id)
        request = _require_request(request_id)
        feedback = str(feedback_type or "").strip().casefold()
        if feedback not in FEEDBACK_TYPES:
            raise LearningFeedbackValidationError("反馈类型无效")
        issue = str(issue_type or "").strip().casefold() or None
        if feedback == "correction" and issue not in ISSUE_TYPES:
            raise LearningFeedbackValidationError("修正类型无效")
        if feedback == "accepted":
            issue = None
        safe_note = sanitize_private_note(note) or None
        if feedback == "accepted":
            safe_note = None
        now = time.time()
        with self._connection() as connection:
            event = connection.execute(
                f"""
                SELECT event_id, conversation_id, question_hash, normalized_question,
                       route_family, question_text
                FROM {self._events_table}
                WHERE principal_hash = ? AND request_id = ?
                """,
                (principal, request),
            ).fetchone()
            if event is None:
                raise LearningFeedbackNotFoundError("该取数请求不存在或已过期")
            if str(event["conversation_id"]) != conversation:
                raise LearningOwnershipError("该取数请求不存在或已过期")
            existing = connection.execute(
                f"""
                SELECT feedback_id, feedback_type, issue_type
                FROM {self._feedback_table}
                WHERE principal_hash = ? AND request_id = ?
                """,
                (principal, request),
            ).fetchone()
            feedback_id = str(existing["feedback_id"]) if existing else "qf_" + secrets.token_urlsafe(18)
            if existing is None:
                connection.execute(
                    f"""
                    INSERT INTO {self._feedback_table}
                        (feedback_id, principal_hash, conversation_id, request_id,
                         event_id, feedback_type, issue_type, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        principal,
                        conversation,
                        request,
                        str(event["event_id"]),
                        feedback,
                        issue,
                        safe_note,
                        now,
                    ),
                )
            else:
                connection.execute(
                    f"""
                    UPDATE {self._feedback_table}
                    SET feedback_type = ?, issue_type = ?, note = ?, created_at = ?
                    WHERE feedback_id = ? AND principal_hash = ? AND request_id = ?
                    """,
                    (feedback, issue, safe_note, now, feedback_id, principal, request),
                )
            old_feedback_type = str(existing["feedback_type"] or "") if existing else ""
            old_issue = str(existing["issue_type"] or "") if existing else ""
            if old_feedback_type == "correction" and old_issue:
                if feedback != "correction" or old_issue != issue:
                    self._remove_candidate_occurrence(
                        connection,
                        principal_hash=principal,
                        candidate_type="correction",
                        candidate_key=f"{event['question_hash']}:{old_issue}",
                    )
            if feedback == "correction":
                candidate_key = f"{event['question_hash']}:{issue}"
                self._upsert_candidate(
                    connection,
                    principal_hash=principal,
                    candidate_type="correction",
                    candidate_key=candidate_key,
                    payload={
                        "normalized_question": str(event["normalized_question"]),
                        "example": str(event["question_text"]),
                        "route_family": str(event["route_family"]),
                        "issue_type": issue,
                        "issue_label": ISSUE_LABELS.get(str(issue), "其他"),
                    },
                    now=now,
                    increment=not (
                        old_feedback_type == "correction" and old_issue == issue
                    ),
                )
        self.build_digest(principal)
        return {"feedback_id": feedback_id, "updated": existing is not None}

    def _rows_for_principal(
        self, connection: sqlite3.Connection, principal_hash: str
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
        events = connection.execute(
            f"""
            SELECT e.*, f.feedback_type, f.issue_type, f.note
            FROM {self._events_table} AS e
            LEFT JOIN {self._feedback_table} AS f
              ON f.principal_hash = e.principal_hash AND f.request_id = e.request_id
            WHERE e.principal_hash = ?
            ORDER BY e.created_at ASC, e.event_id ASC
            """,
            (principal_hash,),
        ).fetchall()
        definitions = connection.execute(
            f"""
            SELECT metric_name, definition_text, example_question, created_at
            FROM {self._definitions_table}
            WHERE principal_hash = ?
            ORDER BY created_at ASC, definition_id ASC
            """,
            (principal_hash,),
        ).fetchall()
        candidates = connection.execute(
            f"""
            SELECT candidate_id, candidate_type, candidate_key, payload_json,
                   occurrence_count, status, first_seen_at, last_seen_at
            FROM {self._candidates_table}
            WHERE principal_hash = ?
            ORDER BY last_seen_at DESC, candidate_id ASC
            """,
            (principal_hash,),
        ).fetchall()
        return list(events), list(definitions), list(candidates)

    @staticmethod
    def _unique_examples(rows: list[sqlite3.Row], *, maximum: int = MAX_PRIVATE_EXAMPLES) -> list[str]:
        seen: set[str] = set()
        examples: list[str] = []
        for row in rows:
            value = str(row["question_text"] or "").strip()
            if value and value not in seen:
                seen.add(value)
                examples.append(value)
            if len(examples) >= maximum:
                break
        return examples

    def _build_digest_payload(
        self,
        principal_hash: str,
        events: list[sqlite3.Row],
        definitions: list[sqlite3.Row],
        candidates: list[sqlite3.Row],
    ) -> dict[str, Any]:
        pattern_groups: dict[str, list[sqlite3.Row]] = {}
        for row in events:
            pattern_groups.setdefault(str(row["question_hash"]), []).append(row)

        patterns: list[dict[str, Any]] = []
        for key, rows in sorted(
            pattern_groups.items(),
            key=lambda item: (-len(item[1]), item[0]),
        ):
            first = rows[0]
            feedback_rows = [row for row in rows if row["feedback_type"]]
            corrections = [
                row for row in feedback_rows if row["feedback_type"] == "correction"
            ]
            accepted = [
                row for row in feedback_rows if row["feedback_type"] == "accepted"
            ]
            patterns.append(
                {
                    "candidate_status": "needs_review",
                    "pattern_id": key,
                    "normalized_question": str(first["normalized_question"]),
                    "route_family": str(first["route_family"]),
                    "route_label": ROUTE_LABELS.get(str(first["route_family"]), "其他"),
                    "occurrences": len(rows),
                    "completed": sum(row["outcome"] == "completed" for row in rows),
                    "failed_or_blocked": sum(
                        row["outcome"] in {"failed", "blocked"} for row in rows
                    ),
                    "clarification_required": sum(
                        row["outcome"] == "clarification_required" for row in rows
                    ),
                    "accepted_feedback": len(accepted),
                    "correction_feedback": len(corrections),
                    "issue_types": sorted(
                        {
                            str(row["issue_type"])
                            for row in corrections
                            if row["issue_type"]
                        }
                    ),
                    "examples": self._unique_examples(rows),
                }
            )

        correction_groups: dict[str, list[sqlite3.Row]] = {}
        for row in events:
            issue = row["issue_type"]
            if row["feedback_type"] == "correction" and issue:
                correction_groups.setdefault(str(issue), []).append(row)
        correction_patterns = [
            {
                "issue_type": issue,
                "issue_label": ISSUE_LABELS.get(issue, "其他"),
                "occurrences": len(rows),
                "examples": self._unique_examples(rows, maximum=3),
                "candidate_status": "needs_review",
            }
            for issue, rows in sorted(
                correction_groups.items(), key=lambda item: (-len(item[1]), item[0])
            )
        ]

        route_counts: dict[str, int] = {}
        for row in events:
            route = str(row["route_family"])
            route_counts[route] = route_counts.get(route, 0) + 1
        route_summary = [
            {"id": route, "label": ROUTE_LABELS.get(route, "其他"), "count": count}
            for route, count in sorted(
                route_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ]

        candidate_summary: list[dict[str, Any]] = []
        for row in candidates:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            candidate_summary.append(
                {
                    "candidate_id": str(row["candidate_id"]),
                    "candidate_type": str(row["candidate_type"]),
                    "status": str(row["status"]),
                    "occurrence_count": int(row["occurrence_count"]),
                    "first_seen_at": float(row["first_seen_at"]),
                    "last_seen_at": float(row["last_seen_at"]),
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )

        return {
            "schema_version": LEARNING_SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "scope": "principal_private_learning_inbox",
            "policy": {
                "direct_production_promotion": False,
                "requires_business_owner_review": True,
                "raw_sql_stored": False,
                "result_rows_stored": False,
            },
            "counts": {
                "questions": len(events),
                "completed": sum(row["outcome"] == "completed" for row in events),
                "failed_or_blocked": sum(
                    row["outcome"] in {"failed", "blocked"} for row in events
                ),
                "clarification_required": sum(
                    row["outcome"] == "clarification_required" for row in events
                ),
                "accepted_feedback": sum(
                    row["feedback_type"] == "accepted" for row in events
                ),
                "correction_feedback": sum(
                    row["feedback_type"] == "correction" for row in events
                ),
                "definition_submissions": len(definitions),
                "pending_candidates": sum(
                    row["status"] == "needs_review" for row in candidates
                ),
            },
            "route_families": route_summary,
            "question_patterns": patterns,
            "correction_patterns": correction_patterns,
            "definition_candidates": [
                {
                    "candidate_status": "needs_review",
                    "metric_name": str(row["metric_name"]),
                    "definition": str(row["definition_text"]),
                    "example_question": row["example_question"],
                }
                for row in definitions[-MAX_PRIVATE_EXAMPLES:]
            ],
            "candidates": candidate_summary,
        }

    def _digest_paths(self, principal_hash: str) -> tuple[Path, Path]:
        safe_name = hashlib.sha256(principal_hash.encode("utf-8")).hexdigest()
        return (
            self.digest_root / f"{safe_name}.json",
            self.digest_root / f"{safe_name}.md",
        )

    @staticmethod
    def _markdown_digest(payload: Mapping[str, Any]) -> str:
        counts = payload.get("counts", {})
        lines = [
            "# 取数测试学习总结",
            "",
            f"生成时间：{payload.get('generated_at', '')}",
            "",
            "## 汇总",
            "",
            f"- 测试问题：{counts.get('questions', 0)}",
            f"- 已完成：{counts.get('completed', 0)}",
            f"- 失败或停止：{counts.get('failed_or_blocked', 0)}",
            f"- 需要口径确认：{counts.get('clarification_required', 0)}",
            f"- 已确认正确：{counts.get('accepted_feedback', 0)}",
            f"- 需要修正：{counts.get('correction_feedback', 0)}",
            f"- 待审核候选：{counts.get('pending_candidates', 0)}",
            "",
            "## 问题类别",
            "",
        ]
        for item in payload.get("route_families", []):
            lines.append(f"- {item.get('label', '其他')}：{item.get('count', 0)}")
        lines.extend(["", "## 候选问法（仅供审核）", ""])
        for pattern in payload.get("question_patterns", [])[:30]:
            examples = "；".join(pattern.get("examples", [])[:3])
            lines.append(
                f"- {pattern.get('route_label', '其他')} / {pattern.get('occurrences', 0)} 次：{examples}"
            )
        lines.extend(
            [
                "",
                "## 安全边界",
                "",
                "候选总结不会自动修改生产指标、来源表、Join、单位或权限；需业务负责人审核后才能形成候选版本。",
                "",
            ]
        )
        return "\n".join(lines)

    def build_digest(self, principal_hash: str) -> dict[str, Any]:
        principal = _require_principal(principal_hash)
        with self._connection() as connection:
            events, definitions, candidates = self._rows_for_principal(connection, principal)
            payload = self._build_digest_payload(
                principal, events, definitions, candidates
            )
        json_path, markdown_path = self._digest_paths(principal)
        for path, content in (
            (json_path, _json(payload)),
            (markdown_path, self._markdown_digest(payload)),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=str(path.parent)
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
        return payload

    def principal_hashes(self) -> list[str]:
        """Return owner keys for local maintenance without returning questions."""

        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT principal_hash FROM {self._events_table}
                UNION
                SELECT principal_hash FROM {self._definitions_table}
                UNION
                SELECT principal_hash FROM {self._feedback_table}
                ORDER BY principal_hash
                """
            ).fetchall()
        return [str(row["principal_hash"]) for row in rows]

    def rebuild_all(self) -> list[dict[str, Any]]:
        """Rebuild private digests for every owner and return aggregate counts."""

        return [self.build_digest(principal) for principal in self.principal_hashes()]

    def public_summary(self, principal_hash: str) -> dict[str, Any]:
        """Return counts and labels only; no question text or candidate details."""

        principal = _require_principal(principal_hash)
        with self._connection() as connection:
            events, definitions, candidates = self._rows_for_principal(connection, principal)
        feedback_types = [str(row["feedback_type"] or "") for row in events]
        route_counts: dict[str, int] = {}
        for row in events:
            route = str(row["route_family"])
            route_counts[route] = route_counts.get(route, 0) + 1
        return {
            "schema_version": LEARNING_SCHEMA_VERSION,
            "status": "ready",
            "question_count": len(events),
            "completed_count": sum(row["outcome"] == "completed" for row in events),
            "failed_or_blocked_count": sum(
                row["outcome"] in {"failed", "blocked"} for row in events
            ),
            "clarification_count": sum(
                row["outcome"] == "clarification_required" for row in events
            ),
            "accepted_count": feedback_types.count("accepted"),
            "correction_count": feedback_types.count("correction"),
            "definition_candidate_count": len(definitions),
            "pending_candidate_count": sum(
                row["status"] == "needs_review" for row in candidates
            ),
            "route_families": [
                {
                    "id": route,
                    "label": ROUTE_LABELS.get(route, "其他"),
                    "count": count,
                }
                for route, count in sorted(
                    route_counts.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "policy": {
                "candidate_only": True,
                "production_auto_update": False,
            },
        }

    def prompt_context(self, principal_hash: str, *, limit: int = 6) -> list[dict[str, Any]]:
        """Return a bounded learning summary, never the previous chat context.

        The model may use this as weak evidence about recurring test patterns
        and confirmed corrections. It is deliberately not a transcript: raw
        questions, result rows, SQL, and prior filters are never replayed.
        """

        principal = _require_principal(principal_hash)
        bounded = max(1, min(int(limit), 12))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT issue_type, note
                FROM {self._feedback_table}
                WHERE principal_hash = ? AND feedback_type = 'correction'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (principal, bounded),
            ).fetchall()
            pattern_rows = connection.execute(
                f"""
                SELECT normalized_question, route_family,
                       COUNT(*) AS occurrences,
                       SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN outcome IN ('failed', 'blocked') THEN 1 ELSE 0 END) AS failed_or_blocked,
                       SUM(CASE WHEN outcome = 'clarification_required' THEN 1 ELSE 0 END) AS clarification_required
                FROM {self._events_table}
                WHERE principal_hash = ?
                GROUP BY normalized_question, route_family
                ORDER BY occurrences DESC, normalized_question ASC
                LIMIT ?
                """,
                (principal, bounded),
            ).fetchall()
        hints: list[dict[str, Any]] = []
        for row in reversed(rows):
            issue = _safe_text(row["issue_type"] or "other", maximum=80)
            note = _safe_text(row["note"] or "", maximum=320)
            if note:
                hints.append({"type": "confirmed_correction", "issue_type": issue, "note": note})
        patterns: list[dict[str, Any]] = []
        for row in pattern_rows:
            normalized = _safe_text(row["normalized_question"] or "", maximum=240)
            if not normalized:
                continue
            patterns.append(
                {
                    "pattern": normalized,
                    "route_family": _safe_text(row["route_family"] or "other", maximum=80),
                    "occurrences": int(row["occurrences"] or 0),
                    "completed": int(row["completed"] or 0),
                    "failed_or_blocked": int(row["failed_or_blocked"] or 0),
                    "clarification_required": int(row["clarification_required"] or 0),
                }
            )
        if patterns:
            hints.append(
                {
                    "type": "learning_summary",
                    "policy": (
                        "仅供本次请求选择候选口径；每次必须重新解析范围，"
                        "不得继承历史仓库、SKU、渠道、日期或单位。"
                    ),
                    "patterns": patterns,
                }
            )
        return hints

    def health(self) -> dict[str, Any]:
        with self._connection() as connection:
            question_count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {self._events_table}"
            ).fetchone()["count"]
            feedback_count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {self._feedback_table}"
            ).fetchone()["count"]
            candidate_count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {self._candidates_table} WHERE status = 'needs_review'"
            ).fetchone()["count"]
        return {
            "ready": True,
            "schema_version": LEARNING_SCHEMA_VERSION,
            "question_event_count": int(question_count),
            "feedback_count": int(feedback_count),
            "pending_candidate_count": int(candidate_count),
            "production_auto_update": False,
        }

    def list_candidates(
        self,
        principal_hash: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        principal = _require_principal(principal_hash)
        bounded = max(1, min(int(limit), 500))
        values: tuple[Any, ...]
        if status:
            query = f"""
                SELECT * FROM {self._candidates_table}
                WHERE principal_hash = ? AND status = ?
                ORDER BY last_seen_at DESC, candidate_id ASC
                LIMIT ?
            """
            values = (principal, status, bounded)
        else:
            query = f"""
                SELECT * FROM {self._candidates_table}
                WHERE principal_hash = ?
                ORDER BY last_seen_at DESC, candidate_id ASC
                LIMIT ?
            """
            values = (principal, bounded)
        with self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            result.append(
                {
                    "candidate_id": str(row["candidate_id"]),
                    "candidate_type": str(row["candidate_type"]),
                    "status": str(row["status"]),
                    "occurrence_count": int(row["occurrence_count"]),
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        return result
