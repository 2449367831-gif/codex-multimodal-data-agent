from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .errors import RuntimeErrorBase


SESSION_COOKIE_NAME = "doris_query_session"
CSRF_COOKIE_NAME = "doris_query_csrf"
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60
DEFAULT_LEASE_TTL_SECONDS = 10 * 60
DEFAULT_MAX_SESSIONS = 10_000
DEFAULT_REQUEST_RECEIPT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_REQUEST_RUNNING_TTL_SECONDS = 20 * 60
# A browser may expose several local aliases, but one server-resolved principal owns
# exactly one server conversation.  The aliases are a presentation concern;
# the persisted Codex context is principal-scoped and must never fork here.
DEFAULT_MAX_CONVERSATIONS = 1
COOKIE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
CONVERSATION_PATTERN = re.compile(r"^c_[A-Za-z0-9_-]{10,120}$")
THREAD_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,200}$")
RECEIPT_PATTERN = re.compile(r"^mf_[A-Za-z0-9_-]{10,120}$")
CLARIFICATION_TOKEN_PATTERN = re.compile(r"^ct_[A-Za-z0-9_-]{20,160}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")

# All app instances in one Python worker may point at the same registry (for
# example a test client per browser alias).  A process-wide lock closes that
# otherwise easy-to-miss SQLite contention path; WAL still coordinates separate
# worker processes.
_PROCESS_DB_LOCK = threading.RLock()


class SessionSecurityError(RuntimeErrorBase):
    code = "session_security_error"
    http_status = 403

    def as_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": {}}


class ConversationNotFoundError(RuntimeErrorBase):
    code = "conversation_not_found"
    http_status = 404

    def as_public_dict(self) -> dict[str, object]:
        # Do not reveal whether an ID belongs to another user.
        return {"code": self.code, "message": "会话不存在或已过期", "details": {}}


class ConversationBusyError(RuntimeErrorBase):
    code = "conversation_busy"
    http_status = 409

    def as_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": "该会话正在执行上一条请求", "details": {}}


class ClarificationPendingError(RuntimeErrorBase):
    code = "clarification_pending"
    http_status = 409

    def as_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": "请先完成当前口径选择", "details": {}}


class SessionLimitError(RuntimeErrorBase):
    code = "session_limit_reached"
    http_status = 429

    def as_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": "当前员工已达到对话数量上限", "details": {}}


class ClarificationValidationError(RuntimeErrorBase):
    code = "clarification_invalid"
    http_status = 422

    def as_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": "澄清选项已失效，请重新发起请求", "details": {}}


class ContextOwnershipError(RuntimeErrorBase):
    code = "context_not_owned"
    http_status = 404

    def as_public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": "对话上下文不存在或已过期", "details": {}}


class RequestIdempotencyConflictError(RuntimeErrorBase):
    code = "idempotency_conflict"
    http_status = 409

    def as_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": "请求凭证已用于另一条请求，请重新提交",
            "details": {},
        }


@dataclass(frozen=True)
class Principal:
    """A server-resolved identity; never construct this from JSON body data."""

    kind: str
    subject: str | None = None

    @property
    def key(self) -> str | None:
        if self.kind == "anonymous":
            return None
        if not self.subject:
            raise ValueError("non-anonymous principals need a subject")
        return f"{self.kind}:{self.subject}"


@dataclass(frozen=True)
class SessionContext:
    token: str
    token_hash: str
    public_ref: str
    csrf_token: str
    is_new: bool
    csrf_cookie_needs_set: bool
    expires_at: float
    principal_hash: str
    principal_ref: str


@dataclass(frozen=True)
class PrincipalContext:
    """Metadata for the one Codex thread shared by a server-resolved principal.

    The table intentionally has no user text, SQL, result rows, or model
    transcript columns. Codex owns its own thread state under an isolated home;
    this registry only maps ownership and lifecycle counters.
    """

    principal_hash: str
    codex_thread_id: str | None
    turn_count: int
    compaction_count: int
    context_revision: int
    status: str
    last_compaction_at: float | None


@dataclass(frozen=True)
class RequestReceipt:
    """A replayable public response receipt, without request content."""

    key_hash: str
    principal_hash: str
    conversation_id: str
    route: str
    fingerprint: str
    request_id: str
    state: str
    response_status: int | None
    response_json: str | None
    created_at: float
    updated_at: float

    def public_payload(self) -> dict[str, Any] | None:
        if not self.response_json:
            return None
        try:
            value = json.loads(self.response_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class RequestClaim:
    receipt: RequestReceipt
    owner: bool


_CONTEXT_STATUS_VALUES = frozenset(
    {"new", "ready", "compacting", "compact_unsupported", "reset", "error"}
)


class ChatSessionStore:
    """SQLite ownership registry for browser sessions and conversations.

    Only ownership metadata is stored. Questions, generated SQL, result rows, and
    credentials are intentionally absent. Conversation ownership is keyed by the
    server-resolved principal, while the HttpOnly session cookie protects the browser
    session and CSRF token. The v2 table names make upgrades from an earlier
    development-only schema non-destructive.
    """

    _sessions_table = "chat_sessions_v2"
    _conversations_table = "chat_conversations_v2"
    _leases_table = "chat_leases_v2"
    _contexts_table = "chat_principal_contexts_v1"
    _context_leases_table = "chat_context_leases_v1"
    _clarifications_table = "chat_clarification_tokens_v1"
    _feedback_table = "chat_metric_feedback_v1"
    _request_receipts_table = "chat_request_receipts_v1"

    def __init__(
        self,
        path: Path,
        *,
        principal_pepper: str | bytes | None = None,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
        request_receipt_ttl_seconds: int = DEFAULT_REQUEST_RECEIPT_TTL_SECONDS,
        request_running_ttl_seconds: int = DEFAULT_REQUEST_RUNNING_TTL_SECONDS,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.session_ttl_seconds = _bounded_int(
            session_ttl_seconds, minimum=300, maximum=7 * 24 * 60 * 60
        )
        self.lease_ttl_seconds = _bounded_int(
            lease_ttl_seconds, minimum=30, maximum=60 * 60
        )
        self.max_sessions = _bounded_int(max_sessions, minimum=10, maximum=1_000_000)
        # Keep the constructor argument for backwards compatibility with old
        # deployments, but enforce the security/continuity invariant at the
        # storage boundary.  A configured value greater than one must not be
        # able to create multiple backend conversations for one principal.
        _bounded_int(max_conversations, minimum=1, maximum=10_000)
        self.max_conversations = 1
        self.request_receipt_ttl_seconds = _bounded_int(
            request_receipt_ttl_seconds, minimum=10 * 60, maximum=7 * 24 * 60 * 60
        )
        self.request_running_ttl_seconds = _bounded_int(
            request_running_ttl_seconds, minimum=5 * 60, maximum=60 * 60
        )
        # The metadata registry is shared by all request threads in a worker.
        # SQLite still serializes writers across processes, but this lock keeps
        # the common in-process path from opening competing write transactions
        # for session refresh, leases, and clarification rotation.
        self._db_lock = _PROCESS_DB_LOCK
        if isinstance(principal_pepper, str):
            principal_pepper = principal_pepper.encode("utf-8")
        self.principal_pepper = principal_pepper or b""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Several workers can start against the same SQLite file at once. WAL
        # setup and the legacy migration both need a brief schema lock; retry
        # only that transient condition so startup remains deterministic.
        for attempt in range(8):
            try:
                self._initialize()
                break
            except sqlite3.OperationalError as exc:
                message = str(exc).casefold()
                if "locked" not in message and "busy" not in message:
                    raise
                if attempt == 7:
                    raise
                time.sleep(0.05 * (2**attempt))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._db_lock:
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
                CREATE TABLE IF NOT EXISTS {self._sessions_table} (
                    token_hash TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    principal_ref TEXT NOT NULL,
                    public_ref TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_v2_expiry
                    ON {self._sessions_table} (expires_at);
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_v2_principal
                    ON {self._sessions_table} (principal_hash, last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS {self._conversations_table} (
                    conversation_id TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    created_by_token_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_conversations_v2_owner
                    ON {self._conversations_table} (principal_hash, last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS {self._leases_table} (
                    conversation_id TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_leases_v2_age
                    ON {self._leases_table} (acquired_at);

                CREATE TABLE IF NOT EXISTS {self._contexts_table} (
                    principal_hash TEXT PRIMARY KEY,
                    codex_thread_id TEXT,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    compaction_count INTEGER NOT NULL DEFAULT 0,
                    context_revision INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'new',
                    last_compaction_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_contexts_v1_seen
                    ON {self._contexts_table} (last_seen_at DESC);

                CREATE TABLE IF NOT EXISTS {self._context_leases_table} (
                    principal_hash TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_context_leases_v1_age
                    ON {self._context_leases_table} (acquired_at);

                CREATE TABLE IF NOT EXISTS {self._clarifications_table} (
                    token_hash TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    question_ids_json TEXT NOT NULL,
                    option_ids_json TEXT NOT NULL,
                    selection_json TEXT NOT NULL,
                    public_questions_json TEXT,
                    source_question TEXT,
                    source_reference_at REAL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_clarifications_v1_expiry
                    ON {self._clarifications_table} (expires_at);

                CREATE TABLE IF NOT EXISTS {self._feedback_table} (
                    receipt_id TEXT PRIMARY KEY,
                    principal_hash TEXT NOT NULL,
                    conversation_id TEXT,
                    metric_name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    example_request TEXT,
                    scope_and_grain TEXT,
                    unit_and_window TEXT,
                    notes TEXT,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_review'
                );
                CREATE INDEX IF NOT EXISTS idx_chat_feedback_v1_owner
                    ON {self._feedback_table} (principal_hash, created_at DESC);

                CREATE TABLE IF NOT EXISTS {self._request_receipts_table} (
                    key_hash TEXT NOT NULL,
                    principal_hash TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_status INTEGER,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (key_hash, principal_hash, conversation_id, route)
                );
                CREATE INDEX IF NOT EXISTS idx_chat_request_receipts_v1_updated
                    ON {self._request_receipts_table} (updated_at);
                CREATE INDEX IF NOT EXISTS idx_chat_request_receipts_v1_owner
                    ON {self._request_receipts_table}
                        (principal_hash, conversation_id, route, updated_at DESC);
                """
            )
            # Keep upgrades non-destructive for the development SQLite file.
            existing_feedback_columns = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({self._feedback_table})"
                ).fetchall()
            }
            for name, declaration in (
                ("scope_and_grain", "TEXT"),
                ("unit_and_window", "TEXT"),
                ("notes", "TEXT"),
            ):
                if name not in existing_feedback_columns:
                    connection.execute(
                        f"ALTER TABLE {self._feedback_table} ADD COLUMN {name} {declaration}"
                    )
            clarification_columns = {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({self._clarifications_table})"
                ).fetchall()
            }
            if "public_questions_json" not in clarification_columns:
                connection.execute(
                    f"ALTER TABLE {self._clarifications_table} ADD COLUMN public_questions_json TEXT"
                )
            if "source_question" not in clarification_columns:
                connection.execute(
                    f"ALTER TABLE {self._clarifications_table} ADD COLUMN source_question TEXT"
                )
            if "source_reference_at" not in clarification_columns:
                connection.execute(
                    f"ALTER TABLE {self._clarifications_table} ADD COLUMN source_reference_at REAL"
                )
            if "report_parameters_json" not in clarification_columns:
                connection.execute(
                    f"ALTER TABLE {self._clarifications_table} ADD COLUMN report_parameters_json TEXT"
                )
            # Older development databases allowed several conversation rows
            # for one principal.  Merge those rows before adding the unique
            # owner index so an upgrade is deterministic and non-destructive
            # for the active Codex context and pending feedback references.
            self._deduplicate_conversations(connection)
            self._ensure_unique_conversation_owner_index(connection)

    def _deduplicate_conversations(self, connection: sqlite3.Connection) -> None:
        """Collapse legacy duplicate rows to one canonical conversation.

        The latest-seen row wins, with creation time and ID as deterministic
        tie breakers.  References that belong to the same principal are moved
        to that row; malformed cross-principal references are removed so they
        cannot become an ownership bypass after the migration.
        """

        principal_rows = connection.execute(
            f"""
            SELECT principal_hash
            FROM {self._conversations_table}
            GROUP BY principal_hash
            """
        ).fetchall()
        for principal_row in principal_rows:
            principal_hash = str(principal_row["principal_hash"])
            rows = connection.execute(
                f"""
                SELECT conversation_id, created_by_token_hash, created_at, last_seen_at
                FROM {self._conversations_table}
                WHERE principal_hash = ?
                ORDER BY last_seen_at DESC, created_at DESC, conversation_id ASC
                """,
                (principal_hash,),
            ).fetchall()
            valid_rows = [
                row for row in rows if _valid_conversation_id(str(row["conversation_id"]))
            ]
            # Keep the newest valid ID. If a legacy database has no valid ID,
            # repair it with a fresh opaque ID before moving references.
            if valid_rows:
                canonical_row = valid_rows[0]
                canonical_id = str(canonical_row["conversation_id"])
            else:
                source_row = rows[0]
                canonical_id = "c_" + secrets.token_urlsafe(18)
                connection.execute(
                    f"""
                    INSERT INTO {self._conversations_table}
                        (conversation_id, principal_hash, created_by_token_hash,
                         created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_id,
                        principal_hash,
                        str(source_row["created_by_token_hash"] or "legacy-migration"),
                        float(source_row["created_at"] or time.time()),
                        float(source_row["last_seen_at"] or time.time()),
                    ),
                )
            canonical_seen = False
            for row in rows:
                duplicate_id = str(row["conversation_id"])
                if duplicate_id == canonical_id and not canonical_seen:
                    canonical_seen = True
                    continue

                # Token rows contain only opaque IDs, but still retain the
                # principal check so a corrupt row cannot be reassigned.
                connection.execute(
                    f"""
                    UPDATE {self._clarifications_table}
                    SET conversation_id = ?
                    WHERE conversation_id = ? AND principal_hash = ?
                    """,
                    (canonical_id, duplicate_id, principal_hash),
                )
                connection.execute(
                    f"""
                    DELETE FROM {self._clarifications_table}
                    WHERE conversation_id = ? AND principal_hash = ?
                    """,
                    (duplicate_id, principal_hash),
                )

                connection.execute(
                    f"""
                    UPDATE {self._feedback_table}
                    SET conversation_id = ?
                    WHERE conversation_id = ? AND principal_hash = ?
                    """,
                    (canonical_id, duplicate_id, principal_hash),
                )
                connection.execute(
                    f"""
                    DELETE FROM {self._feedback_table}
                    WHERE conversation_id = ? AND principal_hash = ?
                    """,
                    (duplicate_id, principal_hash),
                )

                duplicate_lease = connection.execute(
                    f"""
                    SELECT principal_hash
                    FROM {self._leases_table}
                    WHERE conversation_id = ?
                    """,
                    (duplicate_id,),
                ).fetchone()
                if duplicate_lease is not None:
                    canonical_lease = connection.execute(
                        f"""
                        SELECT 1
                        FROM {self._leases_table}
                        WHERE conversation_id = ?
                        """,
                        (canonical_id,),
                    ).fetchone()
                    if canonical_lease is None and str(duplicate_lease["principal_hash"]) == principal_hash:
                        try:
                            connection.execute(
                                f"""
                                UPDATE {self._leases_table}
                                SET conversation_id = ?
                                WHERE conversation_id = ? AND principal_hash = ?
                                """,
                                (canonical_id, duplicate_id, principal_hash),
                            )
                        except sqlite3.IntegrityError:
                            # A concurrent/legacy duplicate lease is
                            # transient; retaining the canonical one is enough.
                            connection.execute(
                                f"""
                                DELETE FROM {self._leases_table}
                                WHERE conversation_id = ? AND principal_hash = ?
                                """,
                                (duplicate_id, principal_hash),
                            )
                    else:
                        connection.execute(
                            f"""
                            DELETE FROM {self._leases_table}
                            WHERE conversation_id = ? AND principal_hash = ?
                            """,
                            (duplicate_id, principal_hash),
                        )

                connection.execute(
                    f"""
                    DELETE FROM {self._conversations_table}
                    WHERE conversation_id = ? AND principal_hash = ?
                    """,
                    (duplicate_id, principal_hash),
                )

            # A legacy database could have issued more than one active token
            # before the principal-level gate existed. Keep the newest pending
            # choice set and consume/delete the rest so aliases see one
            # unambiguous contract after restart.
            pending_tokens = connection.execute(
                f"""
                SELECT token_hash
                FROM {self._clarifications_table}
                WHERE principal_hash = ? AND conversation_id = ?
                  AND expires_at > ?
                ORDER BY expires_at DESC, created_at DESC, token_hash ASC
                """,
                (principal_hash, canonical_id, time.time()),
            ).fetchall()
            for stale_token in pending_tokens[1:]:
                connection.execute(
                    f"DELETE FROM {self._clarifications_table} WHERE token_hash = ?",
                    (stale_token["token_hash"],),
                )

    def _ensure_unique_conversation_owner_index(
        self, connection: sqlite3.Connection
    ) -> None:
        """Create/repair the principal uniqueness index after legacy upgrades."""

        index_name = "ux_chat_conversations_v2_principal"
        existing = connection.execute(
            f"PRAGMA index_list({self._conversations_table})"
        ).fetchall()
        for row in existing:
            if str(row[1]) != index_name:
                continue
            columns = [
                str(info[2])
                for info in connection.execute(f"PRAGMA index_info({index_name})").fetchall()
            ]
            if bool(row[2]) and columns == ["principal_hash"]:
                return
            # IF NOT EXISTS would leave a same-named non-unique index in place.
            connection.execute(f"DROP INDEX {index_name}")
            break
        connection.execute(
            f"""
            CREATE UNIQUE INDEX {index_name}
            ON {self._conversations_table} (principal_hash)
            """
        )

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _principal_hash(self, principal_key: str) -> str:
        if self.principal_pepper:
            return hmac.new(
                self.principal_pepper, principal_key.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        return self._hash(principal_key)

    @staticmethod
    def _public_ref(value_hash: str) -> str:
        return value_hash[:16]

    def _cleanup(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            f"DELETE FROM {self._leases_table} WHERE acquired_at <= ?",
            (now - self.lease_ttl_seconds,),
        )
        connection.execute(
            f"DELETE FROM {self._sessions_table} WHERE expires_at <= ?",
            (now,),
        )
        # Conversation ownership is principal-scoped and intentionally outlives
        # a browser cookie.  Removing it with the 8-hour session TTL would make
        # a page refresh allocate a new backend ID while the Codex context was
        # still alive.  Retention/erasure belongs to an explicit admin job.
        connection.execute(
            f"DELETE FROM {self._clarifications_table} WHERE expires_at <= ?",
            (now,),
        )
        connection.execute(
            f"""
            DELETE FROM {self._request_receipts_table}
            WHERE (state = 'running' AND updated_at <= ?)
               OR (state <> 'running' AND updated_at <= ?)
            """,
            (
                now - self.request_running_ttl_seconds,
                now - self.request_receipt_ttl_seconds,
            ),
        )

    def recover_after_restart(self) -> dict[str, int]:
        """Clear transient ownership state left by an interrupted worker.

        The local launcher deliberately runs one chat worker per installation.
        Leases and ``running`` receipts are process-lifetime claims, so keeping
        them after a worker restart can make the next request look permanently
        busy even though no code is executing.  Conversation rows, clarification
        tokens, and Codex context files are durable and are intentionally left
        untouched.  This method is called before the FastAPI worker accepts
        requests.
        """

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running_receipts = connection.execute(
                f"DELETE FROM {self._request_receipts_table} WHERE state = 'running'"
            ).rowcount
            conversation_leases = connection.execute(
                f"DELETE FROM {self._leases_table}"
            ).rowcount
            context_leases = connection.execute(
                f"DELETE FROM {self._context_leases_table}"
            ).rowcount
            compacting_contexts = connection.execute(
                f"""
                UPDATE {self._contexts_table}
                SET status = CASE
                    WHEN codex_thread_id IS NULL THEN 'new'
                    ELSE 'ready'
                END
                WHERE status = 'compacting'
                """
            ).rowcount
        return {
            "running_receipts": max(0, int(running_receipts or 0)),
            "conversation_leases": max(0, int(conversation_leases or 0)),
            "context_leases": max(0, int(context_leases or 0)),
            "compacting_contexts": max(0, int(compacting_contexts or 0)),
        }

    def _trim_sessions(
        self, connection: sqlite3.Connection, *, current_token_hash: str
    ) -> None:
        overflow = connection.execute(
            f"SELECT COUNT(*) - ? AS overflow FROM {self._sessions_table}",
            (self.max_sessions,),
        ).fetchone()["overflow"]
        if overflow and overflow > 0:
            connection.execute(
                f"""
                DELETE FROM {self._sessions_table}
                WHERE token_hash IN (
                    SELECT token_hash
                    FROM {self._sessions_table}
                    WHERE token_hash <> ?
                    ORDER BY last_seen_at ASC
                    LIMIT ?
                )
                """,
                (current_token_hash, int(overflow)),
            )

    def _effective_principal_key(
        self, principal: Principal | None, *, token: str
    ) -> tuple[str, str]:
        if principal is None or principal.kind == "anonymous":
            key = f"anonymous:{token}"
            return key, "anonymous"
        key = principal.key
        if not key:
            raise ValueError("non-anonymous principal has no key")
        return key, principal.kind

    def ensure_session(
        self,
        session_cookie: str | None,
        csrf_cookie: str | None,
        principal: Principal | None = None,
        *,
        allow_create: bool = True,
    ) -> SessionContext:
        now = time.time()
        candidate = session_cookie if _valid_cookie(session_cookie) else None
        candidate_hash = self._hash(candidate) if candidate else None
        expected_principal_hash = None
        if candidate and (principal is None or principal.kind == "anonymous"):
            expected_principal_hash = self._principal_hash(f"anonymous:{candidate}")
        elif principal is not None:
            if not principal.key:
                raise ValueError("non-anonymous principal has no key")
            expected_principal_hash = self._principal_hash(principal.key)

        with self._connection() as connection:
            self._cleanup(connection, now)
            row = None
            if candidate_hash:
                row = connection.execute(
                    f"SELECT * FROM {self._sessions_table} WHERE token_hash = ?",
                    (candidate_hash,),
                ).fetchone()
                if row and (
                    float(row["expires_at"]) <= now
                    or (
                        expected_principal_hash is not None
                        and not hmac.compare_digest(
                            row["principal_hash"], expected_principal_hash
                        )
                    )
                ):
                    row = None

            if row:
                token = candidate
                token_hash = candidate_hash
                csrf_token = csrf_cookie if _valid_cookie(csrf_cookie) else None
                csrf_needs_set = not csrf_token or not hmac.compare_digest(
                    self._hash(csrf_token), row["csrf_hash"]
                )
                if csrf_needs_set:
                    csrf_token = secrets.token_urlsafe(32)
                    csrf_hash = self._hash(csrf_token)
                else:
                    csrf_hash = row["csrf_hash"]
                expires_at = now + self.session_ttl_seconds
                connection.execute(
                    f"""
                    UPDATE {self._sessions_table}
                    SET csrf_hash = ?, last_seen_at = ?, expires_at = ?
                    WHERE token_hash = ?
                    """,
                    (csrf_hash, now, expires_at, token_hash),
                )
                return SessionContext(
                    token=token,
                    token_hash=token_hash,
                    public_ref=row["public_ref"],
                    csrf_token=csrf_token,
                    is_new=False,
                    csrf_cookie_needs_set=csrf_needs_set,
                    expires_at=expires_at,
                    principal_hash=row["principal_hash"],
                    principal_ref=row["principal_ref"],
                )

            if not allow_create:
                raise SessionSecurityError("会话已失效，请刷新页面重试")

            token = secrets.token_urlsafe(32)
            token_hash = self._hash(token)
            principal_key, principal_kind = self._effective_principal_key(
                principal, token=token
            )
            principal_hash = self._principal_hash(principal_key)
            principal_ref = self._public_ref(principal_hash)
            csrf_token = secrets.token_urlsafe(32)
            expires_at = now + self.session_ttl_seconds
            connection.execute(
                f"""
                INSERT INTO {self._sessions_table}
                    (token_hash, principal_hash, principal_ref, public_ref,
                     csrf_hash, created_at, last_seen_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    principal_hash,
                    principal_kind + ":" + principal_ref,
                    self._public_ref(token_hash),
                    self._hash(csrf_token),
                    now,
                    now,
                    expires_at,
                ),
            )
            self._trim_sessions(connection, current_token_hash=token_hash)
            return SessionContext(
                token=token,
                token_hash=token_hash,
                public_ref=self._public_ref(token_hash),
                csrf_token=csrf_token,
                is_new=True,
                csrf_cookie_needs_set=True,
                expires_at=expires_at,
                principal_hash=principal_hash,
                principal_ref=principal_kind + ":" + principal_ref,
            )

    def require_csrf(self, session: SessionContext, header_token: str | None) -> None:
        if not header_token or not _valid_cookie(header_token):
            raise SessionSecurityError("会话校验失败，请刷新页面重试")
        if not hmac.compare_digest(self._hash(header_token), self._hash(session.csrf_token)):
            raise SessionSecurityError("会话校验失败，请刷新页面重试")

    def create_conversation(self, session: SessionContext) -> str:
        now = time.time()
        with self._connection() as connection:
            # Serialize the read-then-create sequence.  The unique index below
            # is the final race guard, while IMMEDIATE keeps normal requests
            # from needlessly colliding under concurrent browser aliases.
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup(connection, now)
            existing = connection.execute(
                f"""
                SELECT conversation_id
                FROM {self._conversations_table}
                WHERE principal_hash = ?
                ORDER BY last_seen_at DESC, created_at DESC, conversation_id ASC
                LIMIT 1
                """,
                (session.principal_hash,),
            ).fetchone()
            if existing is not None:
                conversation_id = str(existing["conversation_id"])
                connection.execute(
                    f"UPDATE {self._conversations_table} SET last_seen_at = ? WHERE conversation_id = ?",
                    (now, conversation_id),
                )
                return conversation_id
            for _ in range(5):
                conversation_id = "c_" + secrets.token_urlsafe(18)
                try:
                    connection.execute(
                        f"""
                        INSERT INTO {self._conversations_table}
                            (conversation_id, principal_hash, created_by_token_hash,
                             created_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            session.principal_hash,
                            session.token_hash,
                            now,
                            now,
                        ),
                    )
                    return conversation_id
                except sqlite3.IntegrityError:
                    # A principal-unique conflict means another request won
                    # the race.  Return its canonical row; an ID collision is
                    # rare and simply retries with a fresh opaque ID.
                    existing = connection.execute(
                        f"""
                        SELECT conversation_id
                        FROM {self._conversations_table}
                        WHERE principal_hash = ?
                        ORDER BY last_seen_at DESC, created_at DESC, conversation_id ASC
                        LIMIT 1
                        """,
                        (session.principal_hash,),
                    ).fetchone()
                    if existing is not None:
                        canonical_id = str(existing["conversation_id"])
                        connection.execute(
                            f"UPDATE {self._conversations_table} SET last_seen_at = ? WHERE conversation_id = ?",
                            (now, canonical_id),
                        )
                        return canonical_id
                    continue
        raise RuntimeError("unable to allocate a conversation id")

    def ensure_default_conversation(self, session: SessionContext) -> str:
        conversation_id = self.find_default_conversation(session)
        if conversation_id:
            return conversation_id
        return self.create_conversation(session)

    def find_default_conversation(self, session: SessionContext) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT conversation_id
                FROM {self._conversations_table}
                WHERE principal_hash = ?
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (session.principal_hash,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    f"UPDATE {self._conversations_table} SET last_seen_at = ? WHERE conversation_id = ?",
                    (time.time(), row["conversation_id"]),
                )
        return row["conversation_id"] if row else None

    def assert_owned(self, session: SessionContext, conversation_id: str) -> str:
        if not _valid_conversation_id(conversation_id):
            raise ConversationNotFoundError("conversation is not owned by this principal")
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT conversation_id
                FROM {self._conversations_table}
                WHERE conversation_id = ? AND principal_hash = ?
                """,
                (conversation_id, session.principal_hash),
            ).fetchone()
            if not row:
                raise ConversationNotFoundError("conversation is not owned by this principal")
            connection.execute(
                f"UPDATE {self._conversations_table} SET last_seen_at = ? WHERE conversation_id = ?",
                (time.time(), conversation_id),
            )
        return conversation_id

    def resolve(self, session: SessionContext, conversation_id: str | None) -> str:
        if conversation_id is None or not conversation_id.strip():
            return self.create_conversation(session)
        return self.assert_owned(session, conversation_id.strip())

    def acquire(self, session: SessionContext, conversation_id: str) -> str:
        self.assert_owned(session, conversation_id)
        now = time.time()
        lease_id = secrets.token_urlsafe(18)
        with self._connection() as connection:
            connection.execute(
                f"DELETE FROM {self._leases_table} WHERE acquired_at <= ?",
                (now - self.lease_ttl_seconds,),
            )
            try:
                connection.execute(
                    f"""
                    INSERT INTO {self._leases_table}
                        (conversation_id, principal_hash, lease_id, acquired_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (conversation_id, session.principal_hash, lease_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationBusyError("conversation is already running") from exc
        return lease_id

    def release(self, session: SessionContext, conversation_id: str, lease_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                f"""
                DELETE FROM {self._leases_table}
                WHERE conversation_id = ? AND principal_hash = ? AND lease_id = ?
                """,
                (conversation_id, session.principal_hash, lease_id),
            )

    def renew(self, session: SessionContext, conversation_id: str, lease_id: str) -> bool:
        """Extend a conversation lease owned by this principal."""

        now = time.time()
        with self._connection() as connection:
            updated = connection.execute(
                f"""
                UPDATE {self._leases_table}
                SET acquired_at = ?
                WHERE conversation_id = ? AND principal_hash = ? AND lease_id = ?
                """,
                (now, conversation_id, session.principal_hash, lease_id),
            ).rowcount
        return bool(updated)

    def acquire_context(self, session: SessionContext) -> str:
        """Serialize all frontend aliases that share one principal Codex thread."""

        now = time.time()
        lease_id = secrets.token_urlsafe(18)
        with self._connection() as connection:
            connection.execute(
                f"DELETE FROM {self._context_leases_table} WHERE acquired_at <= ?",
                (now - self.lease_ttl_seconds,),
            )
            try:
                connection.execute(
                    f"""
                    INSERT INTO {self._context_leases_table}
                        (principal_hash, lease_id, acquired_at)
                    VALUES (?, ?, ?)
                    """,
                    (session.principal_hash, lease_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationBusyError("principal context is already running") from exc
        return lease_id

    def release_context(self, session: SessionContext, lease_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                f"""
                DELETE FROM {self._context_leases_table}
                WHERE principal_hash = ? AND lease_id = ?
                """,
                (session.principal_hash, lease_id),
            )

    def renew_context(self, session: SessionContext, lease_id: str) -> bool:
        """Extend the principal-context lease owned by this principal."""

        now = time.time()
        with self._connection() as connection:
            updated = connection.execute(
                f"""
                UPDATE {self._context_leases_table}
                SET acquired_at = ?
                WHERE principal_hash = ? AND lease_id = ?
                """,
                (now, session.principal_hash, lease_id),
            ).rowcount
        return bool(updated)

    @staticmethod
    def _context_from_row(row: sqlite3.Row) -> PrincipalContext:
        return PrincipalContext(
            principal_hash=str(row["principal_hash"]),
            codex_thread_id=(str(row["codex_thread_id"]) if row["codex_thread_id"] else None),
            turn_count=max(0, int(row["turn_count"] or 0)),
            compaction_count=max(0, int(row["compaction_count"] or 0)),
            context_revision=max(0, int(row["context_revision"] or 0)),
            status=(str(row["status"] or "new") if row["status"] else "new"),
            last_compaction_at=(
                float(row["last_compaction_at"])
                if row["last_compaction_at"] is not None
                else None
            ),
        )

    def _ensure_context_row(
        self, connection: sqlite3.Connection, session: SessionContext, now: float
    ) -> sqlite3.Row:
        connection.execute(
            f"""
            INSERT INTO {self._contexts_table}
                (principal_hash, created_at, last_seen_at, status)
            VALUES (?, ?, ?, 'new')
            ON CONFLICT(principal_hash) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (session.principal_hash, now, now),
        )
        row = connection.execute(
            f"SELECT * FROM {self._contexts_table} WHERE principal_hash = ?",
            (session.principal_hash,),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise ContextOwnershipError("context row unavailable")
        return row

    def context(self, session: SessionContext) -> PrincipalContext:
        now = time.time()
        with self._connection() as connection:
            return self._context_from_row(self._ensure_context_row(connection, session, now))

    def context_home_ref(self, session: SessionContext) -> str:
        """Return a path-safe, non-reversible principal partition identifier."""

        return re.sub(r"[^A-Za-z0-9_-]", "", session.principal_hash)[:48]

    def bind_context_thread(
        self,
        session: SessionContext,
        thread_id: str,
        *,
        replace: bool = False,
        status: str = "ready",
    ) -> PrincipalContext:
        if not isinstance(thread_id, str) or not THREAD_PATTERN.fullmatch(thread_id):
            raise ContextOwnershipError("invalid Codex context id")
        if status not in _CONTEXT_STATUS_VALUES:
            raise ValueError("invalid context status")
        now = time.time()
        with self._connection() as connection:
            row = self._ensure_context_row(connection, session, now)
            existing = row["codex_thread_id"]
            if existing and not replace:
                if not hmac.compare_digest(str(existing), thread_id):
                    return self._context_from_row(row)
                connection.execute(
                    f"UPDATE {self._contexts_table} SET last_seen_at = ?, status = ? WHERE principal_hash = ?",
                    (now, status, session.principal_hash),
                )
            else:
                connection.execute(
                    f"""
                    UPDATE {self._contexts_table}
                    SET codex_thread_id = ?, last_seen_at = ?, status = ?,
                        context_revision = context_revision + 1
                    WHERE principal_hash = ?
                    """,
                    (thread_id, now, status, session.principal_hash),
                )
            fresh = connection.execute(
                f"SELECT * FROM {self._contexts_table} WHERE principal_hash = ?",
                (session.principal_hash,),
            ).fetchone()
            if fresh is None:  # pragma: no cover
                raise ContextOwnershipError("context row unavailable")
            return self._context_from_row(fresh)

    def record_context_turn(
        self,
        session: SessionContext,
        thread_id: str,
        *,
        compaction_status: str | None = None,
    ) -> PrincipalContext:
        now = time.time()
        with self._connection() as connection:
            row = self._ensure_context_row(connection, session, now)
            stored = row["codex_thread_id"]
            if not stored or not hmac.compare_digest(str(stored), thread_id):
                raise ContextOwnershipError("context thread is not owned by this principal")
            status = compaction_status or "ready"
            if status not in _CONTEXT_STATUS_VALUES:
                status = "ready"
            connection.execute(
                f"""
                UPDATE {self._contexts_table}
                SET last_seen_at = ?, turn_count = turn_count + 1, status = ?
                WHERE principal_hash = ? AND codex_thread_id = ?
                """,
                (now, status, session.principal_hash, thread_id),
            )
            fresh = connection.execute(
                f"SELECT * FROM {self._contexts_table} WHERE principal_hash = ?",
                (session.principal_hash,),
            ).fetchone()
            if fresh is None:  # pragma: no cover
                raise ContextOwnershipError("context row unavailable")
            return self._context_from_row(fresh)

    def record_context_compaction(
        self,
        session: SessionContext,
        thread_id: str,
        *,
        status: str = "ready",
    ) -> PrincipalContext:
        now = time.time()
        if status not in _CONTEXT_STATUS_VALUES:
            status = "ready"
        with self._connection() as connection:
            row = self._ensure_context_row(connection, session, now)
            stored = row["codex_thread_id"]
            if not stored or not hmac.compare_digest(str(stored), thread_id):
                raise ContextOwnershipError("context thread is not owned by this principal")
            connection.execute(
                f"""
                UPDATE {self._contexts_table}
                SET last_seen_at = ?, compaction_count = compaction_count + 1,
                    last_compaction_at = ?, status = ?
                WHERE principal_hash = ? AND codex_thread_id = ?
                """,
                (now, now, status, session.principal_hash, thread_id),
            )
            fresh = connection.execute(
                f"SELECT * FROM {self._contexts_table} WHERE principal_hash = ?",
                (session.principal_hash,),
            ).fetchone()
            if fresh is None:  # pragma: no cover
                raise ContextOwnershipError("context row unavailable")
            return self._context_from_row(fresh)

    @staticmethod
    def public_context_state(context: PrincipalContext) -> dict[str, object]:
        """Expose lifecycle counters only; never expose the Codex thread id."""

        # Keep storage-only lifecycle values behind a small public contract.
        # This also prevents a future model/runtime status from becoming a CSS
        # class or user-visible detail by accident.
        public_status = {
            "new": "new",
            "ready": "ready",
            "compacting": "compacting",
            "compact_unsupported": "unsupported",
            "reset": "reset",
            "error": "error",
        }.get(str(context.status).casefold(), "ready")
        return {
            "scope": "principal",
            "status": public_status,
            "turn_count": context.turn_count,
            "compaction_count": context.compaction_count,
            "compaction_pending": public_status == "compacting",
            "context_revision": context.context_revision,
            "has_context": bool(context.codex_thread_id),
            # Natural-language queries never restore this stored context. The
            # row remains only for explicit rule/definition workflows and
            # backward-compatible lifecycle bookkeeping.
            "query_context_mode": "fresh_per_request",
            "learning_summary_only": True,
        }

    def create_clarification_token(
        self,
        session: SessionContext,
        conversation_id: str,
        questions: Sequence[Mapping[str, Any]],
        *,
        ttl_seconds: int = 30 * 60,
        source_question: str | None = None,
        source_reference_at: float | None = None,
        report_parameters: Mapping[str, Any] | None = None,
    ) -> str:
        self.assert_owned(session, conversation_id)
        report_json = None
        if report_parameters is not None:
            if (set(report_parameters) != {"window_days", "end_date_exclusive"}
                    or type(report_parameters["window_days"]) is not int
                    or not 1 <= report_parameters["window_days"] <= 3650):
                raise ClarificationValidationError("invalid report parameters")
            from datetime import date
            try:
                date.fromisoformat(report_parameters["end_date_exclusive"])
            except (TypeError, ValueError):
                raise ClarificationValidationError("invalid report end date") from None
            report_json = json.dumps(dict(report_parameters), separators=(",", ":"))
        if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
            raise ClarificationValidationError("clarification questions must be a list")
        if not questions or len(questions) > 12:
            raise ClarificationValidationError("clarification question count is invalid")
        source_question_value = (
            source_question.strip()[:4000]
            if isinstance(source_question, str)
            else ""
        )
        # Re-run the public interaction sanitizer at the persistence boundary.
        # This keeps a direct store caller from placing model text, source
        # details, or arbitrary IDs into the recoverable browser contract.
        try:
            from .codex_agent import (
                _INTERACTION_LABEL_BY_ID,
                _INTERACTION_OPTION_ID_ALIASES,
                _INTERACTION_PROMPT_BY_ID,
                _INTERACTION_QUESTION_ID_ALIASES,
                interaction_envelope,
            )

            # Older store callers sometimes supplied only semantic IDs. Fill
            # omitted copy from the reviewed semantic catalog when possible;
            # an unknown/missing label remains invalid instead of becoming a
            # misleading placeholder in the browser.
            compatibility_questions: list[Any] = []
            for raw_question in questions:
                if not isinstance(raw_question, Mapping):
                    compatibility_questions.append(raw_question)
                    continue
                question_copy = dict(raw_question)
                if not isinstance(question_copy.get("prompt"), str) or not question_copy.get("prompt", "").strip():
                    question_id = str(question_copy.get("id") or "").strip()
                    canonical_question_id = _INTERACTION_QUESTION_ID_ALIASES.get(
                        question_id, question_id
                    )
                    reviewed_prompt = _INTERACTION_PROMPT_BY_ID.get(canonical_question_id)
                    if reviewed_prompt:
                        question_copy["prompt"] = reviewed_prompt
                raw_options = question_copy.get("options")
                if isinstance(raw_options, list):
                    option_copies: list[Any] = []
                    for raw_option in raw_options:
                        if isinstance(raw_option, Mapping):
                            option_copy = dict(raw_option)
                            if not isinstance(option_copy.get("label"), str) or not option_copy.get("label", "").strip():
                                option_id = str(option_copy.get("id") or "").strip()
                                canonical_option_id = _INTERACTION_OPTION_ID_ALIASES.get(
                                    option_id, option_id
                                )
                                reviewed_label = _INTERACTION_LABEL_BY_ID.get(canonical_option_id)
                                if reviewed_label:
                                    option_copy["label"] = reviewed_label
                            option_copies.append(option_copy)
                        else:
                            option_copies.append(raw_option)
                    question_copy["options"] = option_copies
                compatibility_questions.append(question_copy)

            safe_envelope = interaction_envelope(
                json.dumps(
                    {"outcome": "clarification_required", "questions": compatibility_questions},
                    ensure_ascii=False,
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ClarificationValidationError("clarification questions malformed") from exc
        if safe_envelope.get("outcome") != "clarification_required":
            raise ClarificationValidationError("clarification questions are not reviewable")
        safe_questions = safe_envelope.get("questions")
        if not isinstance(safe_questions, list) or not safe_questions:
            raise ClarificationValidationError("clarification has no selectable options")
        normalized: list[tuple[str, list[str], str]] = []
        seen_question_ids: set[str] = set()
        for question in safe_questions:
            if not isinstance(question, Mapping):
                raise ClarificationValidationError("clarification question malformed")
            question_id = str(question.get("id") or "").strip()
            options = question.get("options")
            if not question_id or not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
                raise ClarificationValidationError("clarification question malformed")
            if len(options) == 0 or len(options) > 20:
                raise ClarificationValidationError("clarification option count is invalid")
            if question_id in seen_question_ids:
                raise ClarificationValidationError("clarification question duplicated")
            seen_question_ids.add(question_id)
            option_ids: list[str] = []
            seen_option_ids: set[str] = set()
            for option in options:
                if not isinstance(option, Mapping):
                    raise ClarificationValidationError("clarification option malformed")
                option_id = str(option.get("id") or "").strip()
                if not option_id:
                    raise ClarificationValidationError("clarification option malformed")
                if option_id in seen_option_ids:
                    raise ClarificationValidationError("clarification option duplicated")
                seen_option_ids.add(option_id)
                option_ids.append(option_id)
            selection = str(question.get("selection") or "single").casefold()
            if selection not in {"single", "multiple"}:
                raise ClarificationValidationError("clarification selection is invalid")
            normalized.append((question_id, option_ids, selection))
        if not normalized:
            raise ClarificationValidationError("clarification has no selectable options")
        token = "ct_" + secrets.token_urlsafe(32)
        now = time.time()
        reference_at = now if source_reference_at is None else source_reference_at
        if isinstance(reference_at, bool) or not isinstance(reference_at, (int, float)) or not math.isfinite(reference_at) or not 0 < reference_at <= now:
            raise ClarificationValidationError("invalid clarification reference time")
        expires = now + max(60, min(int(ttl_seconds), 24 * 60 * 60))
        question_ids = [item[0] for item in normalized]
        options_by_question = {item[0]: item[1] for item in normalized}
        selections = {item[0]: item[2] for item in normalized}
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # There is only one actionable clarification for a shared
            # principal context. Replace any stale/duplicate token atomically
            # before inserting the new one, so aliases cannot race with two
            # different answer sets.
            connection.execute(
                f"""
                DELETE FROM {self._clarifications_table}
                WHERE principal_hash = ? AND conversation_id = ?
                """,
                (session.principal_hash, conversation_id),
            )
            connection.execute(
                f"""
                INSERT INTO {self._clarifications_table}
                    (token_hash, principal_hash, conversation_id, question_ids_json,
                     option_ids_json, selection_json, public_questions_json,
                     source_question, source_reference_at, created_at, expires_at, report_parameters_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._hash(token),
                    session.principal_hash,
                    conversation_id,
                    json.dumps(question_ids, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(options_by_question, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(selections, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(safe_questions, ensure_ascii=False, separators=(",", ":")),
                    source_question_value,
                    reference_at,
                    now,
                    expires,
                    report_json,
                ),
            )
        return token

    def has_pending_clarification(
        self, session: SessionContext, conversation_id: str
    ) -> bool:
        """Return whether this principal must finish a choice before a new turn.

        The check is principal-bound and expires stale tokens in the same
        transaction.  This prevents a second browser alias from writing a new
        turn into the shared Codex thread while another alias still has an
        unconsumed clarification.
        """

        self.assert_owned(session, conversation_id)
        now = time.time()
        with self._connection() as connection:
            self._cleanup(connection, now)
            row = connection.execute(
                f"""
                SELECT 1
                FROM {self._clarifications_table}
                WHERE principal_hash = ? AND conversation_id = ? AND expires_at > ?
                LIMIT 1
                """,
                (session.principal_hash, conversation_id, now),
            ).fetchone()
        return row is not None

    def discard_pending_clarifications(
        self, session: SessionContext, conversation_id: str
    ) -> int:
        """Discard pending cards when the user explicitly starts a new question.

        A clarification belongs to the question that created it.  A new
        free-form question is therefore a replacement of that unfinished
        branch, not an answer to it.  Keep the delete principal- and
        conversation-bound so one installation can never affect another.
        """

        self.assert_owned(session, conversation_id)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                DELETE FROM {self._clarifications_table}
                WHERE principal_hash = ? AND conversation_id = ?
                """,
                (session.principal_hash, conversation_id),
            )
            return max(0, int(cursor.rowcount or 0))

    def resume_clarification(
        self, session: SessionContext, conversation_id: str
    ) -> dict[str, Any] | None:
        """Return a fresh one-time token and safe questions for a pending turn.

        The token value is never stored in plaintext.  A short-lived sibling
        alias can receive a new hash for the same reviewed question set; any
        successful answer consumes all sibling tokens for this principal.
        """

        self.assert_owned(session, conversation_id)
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup(connection, now)
            row = connection.execute(
                f"""
                SELECT *
                FROM {self._clarifications_table}
                WHERE principal_hash = ? AND conversation_id = ? AND expires_at > ?
                ORDER BY expires_at DESC, created_at DESC, token_hash ASC
                LIMIT 1
                """,
                (session.principal_hash, conversation_id, now),
            ).fetchone()
            if row is None:
                return None

            def invalid_pending_row() -> None:
                # Never delete a row belonging to another principal, even if a
                # legacy database contains a malformed cross-owner reference.
                connection.execute(
                    f"""
                    DELETE FROM {self._clarifications_table}
                    WHERE token_hash = ? AND principal_hash = ? AND conversation_id = ?
                    """,
                    (row["token_hash"], session.principal_hash, conversation_id),
                )

            try:
                question_ids = json.loads(row["question_ids_json"])
                options_by_question = json.loads(row["option_ids_json"])
                selections = json.loads(row["selection_json"])
                stored_public_questions = (
                    json.loads(row["public_questions_json"])
                    if row["public_questions_json"]
                    else None
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid_pending_row()
                return None

            if (
                not isinstance(question_ids, list)
                or not question_ids
                or len(question_ids) > 12
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in question_ids
                )
                or len({str(value) for value in question_ids}) != len(question_ids)
                or not isinstance(options_by_question, dict)
                or not isinstance(selections, dict)
            ):
                invalid_pending_row()
                return None
            question_ids = [str(value).strip() for value in question_ids]
            if set(options_by_question) != set(question_ids) or set(selections) != set(question_ids):
                invalid_pending_row()
                return None

            normalized_options: dict[str, list[str]] = {}
            normalized_selections: dict[str, str] = {}
            for question_id in question_ids:
                raw_options = options_by_question.get(question_id)
                selection = str(selections.get(question_id) or "single").casefold()
                if (
                    not isinstance(raw_options, list)
                    or not raw_options
                    or len(raw_options) > 20
                    or selection not in {"single", "multiple"}
                ):
                    invalid_pending_row()
                    return None
                option_ids = [str(value).strip() for value in raw_options]
                if (
                    any(not value for value in option_ids)
                    or len(set(option_ids)) != len(option_ids)
                ):
                    invalid_pending_row()
                    return None
                normalized_options[question_id] = option_ids
                normalized_selections[question_id] = selection

            # Legacy rows may not have retained public copy.  Either way,
            # sanitize the candidate again and verify that its opaque contract
            # still exactly matches the token before issuing a fresh token.
            if isinstance(stored_public_questions, list):
                candidate_questions: Any = stored_public_questions
            else:
                candidate_questions = None
            try:
                from .codex_agent import (
                    _INTERACTION_LABEL_BY_ID,
                    _INTERACTION_OPTION_ID_ALIASES,
                    _INTERACTION_OPTION_ORDER_BY_QUESTION,
                    _INTERACTION_PROMPT_BY_ID,
                    interaction_envelope,
                )

                # Rebuild a legacy card from semantic IDs when possible. A
                # positional-only row is recoverable only for a reviewed
                # question with a fixed option order; otherwise it is retired
                # instead of guessing the business meaning.
                def semantic_candidate_questions() -> list[dict[str, Any]]:
                    unavailable_options = {
                        "outbound_transfer", "outbound_adjustment", "outbound_other", "outbound_all",
                        "outbound_allocated_qty", "outbound_requested_qty", "outbound_order_count",
                        "outbound_order_created_at", "outbound_reviewed_at",
                    }
                    result: list[dict[str, Any]] = []
                    for question_id in question_ids:
                        ordered_ids = _INTERACTION_OPTION_ORDER_BY_QUESTION.get(question_id, ())
                        options: list[dict[str, Any]] = []
                        for index, option_id in enumerate(normalized_options[question_id]):
                            canonical_option_id = _INTERACTION_OPTION_ID_ALIASES.get(
                                option_id, option_id
                            )
                            reviewed_id = canonical_option_id
                            if not _INTERACTION_LABEL_BY_ID.get(reviewed_id) and index < len(ordered_ids):
                                reviewed_id = ordered_ids[index]
                            options.append(
                                {
                                    # Keep the opaque ID recorded in the token;
                                    # only the browser label is recovered from
                                    # the reviewed semantic vocabulary.
                                    "id": option_id,
                                    "label": _INTERACTION_LABEL_BY_ID.get(reviewed_id),
                                    "available": option_id not in unavailable_options,
                                }
                            )
                        result.append(
                            {
                                "id": question_id,
                                "prompt": _INTERACTION_PROMPT_BY_ID.get(
                                    question_id, "请确认本次业务口径。"
                                ),
                                "selection": normalized_selections[question_id],
                                "options": options,
                            }
                        )
                    return result

                def has_unusable_questions(value: Any) -> bool:
                    if not isinstance(value, list):
                        return True
                    return any(
                        not isinstance(option, Mapping)
                        or not str(option.get("label") or "").strip()
                        for question in value
                        if isinstance(question, Mapping)
                        for option in (question.get("options") or [])
                    )

                if candidate_questions is None:
                    candidate_questions = semantic_candidate_questions()

                safe_envelope = interaction_envelope(
                    json.dumps(
                        {
                            "outcome": "clarification_required",
                            "questions": candidate_questions,
                        },
                        ensure_ascii=False,
                    )
                )
                # An old card can contain a stale/garbled label even though
                # its token metadata is still parseable. Retry once from the
                # reviewed semantic catalog before retiring it.
                if has_unusable_questions(safe_envelope.get("questions")):
                    candidate_questions = semantic_candidate_questions()
                    safe_envelope = interaction_envelope(
                        json.dumps(
                            {
                                "outcome": "clarification_required",
                                "questions": candidate_questions,
                            },
                            ensure_ascii=False,
                        )
                    )
            except Exception:
                invalid_pending_row()
                return None
            public_questions = safe_envelope.get("questions")
            if safe_envelope.get("outcome") != "clarification_required" or not isinstance(public_questions, list):
                invalid_pending_row()
                return None
            if any(
                not isinstance(option, Mapping)
                or not str(option.get("label") or "").strip()
                for public_question in public_questions
                if isinstance(public_question, Mapping)
                for option in (public_question.get("options") or [])
            ):
                # Never return a generic option to the browser. The original
                # user text is intentionally not stored, so an unrecoverable
                # legacy card must be resubmitted rather than guessed.
                invalid_pending_row()
                return None
            if len(public_questions) != len(question_ids):
                invalid_pending_row()
                return None
            for question_id, public_question in zip(question_ids, public_questions):
                if not isinstance(public_question, Mapping):
                    invalid_pending_row()
                    return None
                if str(public_question.get("id") or "") != question_id:
                    invalid_pending_row()
                    return None
                if str(public_question.get("selection") or "single").casefold() != normalized_selections[question_id]:
                    invalid_pending_row()
                    return None
                public_options = public_question.get("options")
                if not isinstance(public_options, list):
                    invalid_pending_row()
                    return None
                public_option_ids = [
                    str(option.get("id") or "")
                    for option in public_options
                    if isinstance(option, Mapping)
                ]
                if public_option_ids != normalized_options[question_id]:
                    invalid_pending_row()
                    return None

            token = "ct_" + secrets.token_urlsafe(32)
            expires_at = float(row["expires_at"])
            connection.execute(
                f"""
                INSERT INTO {self._clarifications_table}
                    (token_hash, principal_hash, conversation_id, question_ids_json,
                    option_ids_json, selection_json, public_questions_json,
                      source_question, source_reference_at, created_at, expires_at, report_parameters_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._hash(token),
                    session.principal_hash,
                    conversation_id,
                    json.dumps(question_ids, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(normalized_options, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(normalized_selections, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(public_questions, ensure_ascii=False, separators=(",", ":")),
                    str(row["source_question"] or "")[:4000]
                    if "source_question" in row.keys()
                    else "",
                    row["source_reference_at"],
                    now,
                    expires_at,
                    row["report_parameters_json"],
                ),
            )
            # Retain issuance order within this write transaction. Timestamps can
            # tie or move backward; random hashes must not evict the new token.
            extra = connection.execute(
                f"""
                SELECT token_hash
                FROM {self._clarifications_table}
                WHERE principal_hash = ? AND conversation_id = ?
                ORDER BY rowid DESC
                LIMIT -1 OFFSET 3
                """,
                (session.principal_hash, conversation_id),
            ).fetchall()
            for stale in extra:
                connection.execute(
                    f"""
                    DELETE FROM {self._clarifications_table}
                    WHERE token_hash = ? AND principal_hash = ? AND conversation_id = ?
                    """,
                    (stale["token_hash"], session.principal_hash, conversation_id),
                )
            return {"token": token, "questions": public_questions,
                    "draft_id": self._clarification_draft_id(row)}

    def _clarification_draft_id(self, row) -> str:
        # A rotation keeps this opaque identity; a new request or contract does not.
        identity = [row[key] for key in ("principal_hash", "conversation_id", "source_reference_at",
                                         "source_question", "question_ids_json", "option_ids_json", "selection_json")]
        return "cd_" + self._hash(json.dumps(identity, ensure_ascii=True, separators=(",", ":")))

    def clarification_draft_id(self, session, conversation_id, token) -> str | None:
        self.assert_owned(session, conversation_id)
        if not isinstance(token, str) or not CLARIFICATION_TOKEN_PATTERN.fullmatch(token):
            return None
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {self._clarifications_table} "
                "WHERE token_hash=? AND principal_hash=? AND conversation_id=? AND expires_at>?",
                (self._hash(token), session.principal_hash, conversation_id, time.time()),
            ).fetchone()
        return self._clarification_draft_id(row) if row is not None else None

    def clarification_reference_time(self, session, conversation_id, token):
        """Read only the server-bound anchor; rotating tokens never resets it."""
        self.assert_owned(session, conversation_id)
        if not isinstance(token, str) or not CLARIFICATION_TOKEN_PATTERN.fullmatch(token):
            return None
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT source_reference_at FROM {self._clarifications_table} "
                "WHERE token_hash=? AND principal_hash=? AND conversation_id=? AND expires_at>?",
                (self._hash(token), session.principal_hash, conversation_id, time.time()),
            ).fetchone()
        value = row["source_reference_at"] if row is not None else None
        return value if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None

    def clarification_report_parameters(self, session, conversation_id, token):
        self.assert_owned(session, conversation_id)
        if not isinstance(token, str) or not CLARIFICATION_TOKEN_PATTERN.fullmatch(token):
            return None
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT report_parameters_json FROM {self._clarifications_table} "
                "WHERE token_hash=? AND principal_hash=? AND conversation_id=? AND expires_at>?",
                (self._hash(token), session.principal_hash, conversation_id, time.time()),
            ).fetchone()
        return json.loads(row["report_parameters_json"]) if row and row["report_parameters_json"] else None

    def clarification_source_question(
        self,
        session: SessionContext,
        conversation_id: str,
        token: str,
    ) -> str | None:
        """Return the original bounded question for an owned pending token."""

        self.assert_owned(session, conversation_id)
        if not isinstance(token, str) or not CLARIFICATION_TOKEN_PATTERN.fullmatch(token):
            return None
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT source_question
                FROM {self._clarifications_table}
                WHERE token_hash = ? AND principal_hash = ? AND conversation_id = ?
                  AND expires_at > ?
                LIMIT 1
                """,
                (self._hash(token), session.principal_hash, conversation_id, now),
            ).fetchone()
        if row is None:
            return None
        value = row["source_question"]
        return value.strip()[:4000] if isinstance(value, str) and value.strip() else None

    def validate_clarification_token(
        self,
        session: SessionContext,
        conversation_id: str,
        token: str,
        answers: Any,
        *,
        include_business_meaning: bool = False,
    ) -> list[dict[str, Any]]:
        self.assert_owned(session, conversation_id)
        if not isinstance(token, str) or not CLARIFICATION_TOKEN_PATTERN.fullmatch(token):
            raise ClarificationValidationError("invalid clarification token")
        if not isinstance(answers, Sequence) or isinstance(answers, (str, bytes)):
            raise ClarificationValidationError("clarification answers must be a list")
        now = time.time()
        with self._connection() as connection:
            # Reserve the row and consume it in one write transaction.  Without
            # this lock, two concurrent requests could both validate the same
            # opaque token before either DELETE commits.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT * FROM {self._clarifications_table}
                WHERE token_hash = ? AND principal_hash = ? AND conversation_id = ?
                  AND expires_at > ?
                """,
                (self._hash(token), session.principal_hash, conversation_id, now),
            ).fetchone()
            if row is None:
                raise ClarificationValidationError("clarification token expired or not owned")
            try:
                question_ids = json.loads(row["question_ids_json"])
                options_by_question = json.loads(row["option_ids_json"])
                selections = json.loads(row["selection_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:  # pragma: no cover
                raise ClarificationValidationError("clarification token malformed") from exc
            if not isinstance(question_ids, list) or not isinstance(options_by_question, dict):
                raise ClarificationValidationError("clarification token malformed")
            normalized: dict[str, list[str]] = {}
            date_ranges: dict[str, dict[str, str]] = {}
            for answer in answers:
                if not isinstance(answer, Mapping):
                    raise ClarificationValidationError("clarification answer malformed")
                question_id = str(answer.get("question_id") or answer.get("id") or "").strip()
                raw_options = answer.get("option_ids")
                if raw_options is None:
                    raw_option = answer.get("option_id")
                    raw_options = [raw_option] if raw_option is not None else []
                if isinstance(raw_options, (str, bytes)):
                    raw_options = [raw_options]
                if not question_id or not isinstance(raw_options, Sequence):
                    raise ClarificationValidationError("clarification answer malformed")
                option_ids = [str(value).strip() for value in raw_options if str(value).strip()]
                if not option_ids or question_id in normalized:
                    raise ClarificationValidationError("clarification answer duplicated")
                if len(option_ids) != len(set(option_ids)):
                    raise ClarificationValidationError("clarification option duplicated")
                allowed = options_by_question.get(question_id)
                if not isinstance(allowed, list) or any(value not in allowed for value in option_ids):
                    raise ClarificationValidationError("clarification option not allowed")
                selection = selections.get(question_id, "single")
                if selection == "single" and len(option_ids) != 1:
                    raise ClarificationValidationError("single-select clarification needs one option")
                from .agent_core.turnover_conflict import validate_date_range
                try:
                    date_range = validate_date_range(question_id, option_ids, answer)
                except ValueError as exc:
                    raise ClarificationValidationError("clarification date range is invalid") from exc
                if date_range is not None:
                    date_ranges[question_id] = date_range
                normalized[question_id] = list(option_ids)
            if set(normalized) != set(str(value) for value in question_ids):
                raise ClarificationValidationError("all clarification questions must be answered")
            # Consume the opaque selection token only after validation succeeds.
            # The surrounding IMMEDIATE transaction makes validation + consume
            # atomic, so a replay cannot race the first request.
            deleted = connection.execute(
                f"""
                DELETE FROM {self._clarifications_table}
                WHERE principal_hash = ? AND conversation_id = ?
                """,
                (session.principal_hash, conversation_id),
            ).rowcount
            if deleted < 1:
                raise ClarificationValidationError("clarification token already used")
            result = [
                {"question_id": question_id, "option_ids": normalized[question_id],
                 **({"date_range": date_ranges[question_id]} if question_id in date_ranges else {})}
                for question_id in question_ids
            ]
            if include_business_meaning:
                # Recover only server-bound wording, never client-provided labels.
                questions = json.loads(row["public_questions_json"] or "[]")
                by_id = {q["id"]: q for q in questions if isinstance(q, dict) and "id" in q}
                for answer in result:
                    question = by_id.get(answer["question_id"], {})
                    answer["prompt"] = str(question.get("prompt") or "")[:500]
                    answer["selected_options"] = [
                        {"id": option["id"], "label": str(option.get("label") or "")[:300]}
                        for option in question.get("options", [])
                        if option.get("id") in answer["option_ids"]
                    ]
            return result

    @staticmethod
    def _request_receipt_from_row(row: sqlite3.Row) -> RequestReceipt:
        return RequestReceipt(
            key_hash=str(row["key_hash"]),
            principal_hash=str(row["principal_hash"]),
            conversation_id=str(row["conversation_id"]),
            route=str(row["route"]),
            fingerprint=str(row["fingerprint"]),
            request_id=str(row["request_id"]),
            state=str(row["state"] or "running"),
            response_status=(
                int(row["response_status"])
                if row["response_status"] is not None
                else None
            ),
            response_json=(
                str(row["response_json"]) if row["response_json"] is not None else None
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def claim_request(
        self,
        session: SessionContext,
        conversation_id: str,
        *,
        route: str,
        idempotency_key: str,
        fingerprint: str,
        request_id: str,
    ) -> RequestClaim:
        """Atomically claim or replay one logical browser request.

        Only hashes and a bounded, already-sanitized public response are retained.
        The request question, generated SQL, result rows, and credentials never
        enter this table. A stale running receipt can be reclaimed, but an old
        worker cannot finish over the newer request because completion is bound to
        its original request ID.
        """

        if not _valid_conversation_id(conversation_id):
            raise ConversationNotFoundError("conversation is not owned by this principal")
        if not isinstance(route, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", route):
            raise ValueError("request route is invalid")
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(
            idempotency_key
        ):
            raise ValueError("idempotency key is invalid")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("request fingerprint is invalid")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,80}", request_id):
            raise ValueError("request id is invalid")
        self.assert_owned(session, conversation_id)
        key_hash = self._hash(f"{route}\x00{idempotency_key}")
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup(connection, now)
            row = connection.execute(
                f"""
                SELECT *
                FROM {self._request_receipts_table}
                WHERE key_hash = ? AND principal_hash = ?
                  AND conversation_id = ? AND route = ?
                """,
                (key_hash, session.principal_hash, conversation_id, route),
            ).fetchone()
            if row is not None:
                existing = self._request_receipt_from_row(row)
                if not hmac.compare_digest(existing.fingerprint, fingerprint):
                    raise RequestIdempotencyConflictError("request key was reused for another body")
                if existing.state == "running":
                    if now - existing.updated_at <= self.request_running_ttl_seconds:
                        return RequestClaim(receipt=existing, owner=False)
                    # A worker can disappear after the process or machine is
                    # restarted. Reclaim only after the generous stale window.
                    connection.execute(
                        f"""
                        UPDATE {self._request_receipts_table}
                        SET request_id = ?, state = 'running', response_status = NULL,
                            response_json = NULL, updated_at = ?
                        WHERE key_hash = ? AND principal_hash = ?
                          AND conversation_id = ? AND route = ?
                          AND request_id = ?
                        """,
                        (
                            request_id,
                            now,
                            key_hash,
                            session.principal_hash,
                            conversation_id,
                            route,
                            existing.request_id,
                        ),
                    )
                    refreshed = connection.execute(
                        f"""
                        SELECT *
                        FROM {self._request_receipts_table}
                        WHERE key_hash = ? AND principal_hash = ?
                          AND conversation_id = ? AND route = ?
                        """,
                        (key_hash, session.principal_hash, conversation_id, route),
                    ).fetchone()
                    if refreshed is None:  # pragma: no cover - guarded by transaction
                        raise RuntimeError("request receipt disappeared during reclaim")
                    return RequestClaim(
                        receipt=self._request_receipt_from_row(refreshed), owner=True
                    )
                # Completed and failed receipts are replayed verbatim. This is
                # what makes a lost HTTP response safe to retry.
                return RequestClaim(receipt=existing, owner=False)

            connection.execute(
                f"""
                INSERT INTO {self._request_receipts_table}
                    (key_hash, principal_hash, conversation_id, route, fingerprint,
                     request_id, state, response_status, response_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'running', NULL, NULL, ?, ?)
                """,
                (
                    key_hash,
                    session.principal_hash,
                    conversation_id,
                    route,
                    fingerprint,
                    request_id,
                    now,
                    now,
                ),
            )
            inserted = connection.execute(
                f"""
                SELECT *
                FROM {self._request_receipts_table}
                WHERE key_hash = ? AND principal_hash = ?
                  AND conversation_id = ? AND route = ?
                """,
                (key_hash, session.principal_hash, conversation_id, route),
            ).fetchone()
            if inserted is None:  # pragma: no cover - guarded by the insert above
                raise RuntimeError("request receipt was not created")
            return RequestClaim(receipt=self._request_receipt_from_row(inserted), owner=True)

    @staticmethod
    def _encode_public_receipt_payload(payload: Mapping[str, Any]) -> str:
        if not isinstance(payload, Mapping):
            raise ValueError("receipt payload must be an object")
        encoded = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        # A receipt is a recovery cache, not a transcript store. Keep it bounded
        # so an accidental provider response cannot grow the session database.
        if len(encoded.encode("utf-8")) > 512 * 1024:
            raise ValueError("receipt payload is too large")
        return encoded

    def _finish_request(
        self,
        session: SessionContext,
        receipt: RequestReceipt,
        *,
        state: str,
        response_status: int,
        payload: Mapping[str, Any],
    ) -> None:
        if state not in {"completed", "failed"}:
            raise ValueError("invalid request receipt state")
        if not 100 <= int(response_status) <= 599:
            raise ValueError("invalid receipt response status")
        encoded = self._encode_public_receipt_payload(payload)
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                f"""
                UPDATE {self._request_receipts_table}
                SET state = ?, response_status = ?, response_json = ?, updated_at = ?
                WHERE key_hash = ? AND principal_hash = ? AND conversation_id = ?
                  AND route = ? AND request_id = ? AND fingerprint = ?
                  AND state = 'running'
                """,
                (
                    state,
                    int(response_status),
                    encoded,
                    now,
                    receipt.key_hash,
                    session.principal_hash,
                    receipt.conversation_id,
                    receipt.route,
                    receipt.request_id,
                    receipt.fingerprint,
                ),
            )

    def complete_request(
        self,
        session: SessionContext,
        receipt: RequestReceipt,
        payload: Mapping[str, Any],
        *,
        response_status: int = 200,
    ) -> None:
        self._finish_request(
            session,
            receipt,
            state="completed",
            response_status=response_status,
            payload=payload,
        )

    def fail_request(
        self,
        session: SessionContext,
        receipt: RequestReceipt,
        payload: Mapping[str, Any],
        *,
        response_status: int,
    ) -> None:
        self._finish_request(
            session,
            receipt,
            state="failed",
            response_status=response_status,
            payload=payload,
        )

    def renew_request(self, session: SessionContext, receipt: RequestReceipt) -> bool:
        """Keep a running request receipt alive while the worker is executing."""

        now = time.time()
        with self._connection() as connection:
            updated = connection.execute(
                f"""
                UPDATE {self._request_receipts_table}
                SET updated_at = ?
                WHERE key_hash = ? AND principal_hash = ? AND conversation_id = ?
                  AND route = ? AND request_id = ? AND fingerprint = ?
                  AND state = 'running'
                """,
                (
                    now,
                    receipt.key_hash,
                    session.principal_hash,
                    receipt.conversation_id,
                    receipt.route,
                    receipt.request_id,
                    receipt.fingerprint,
                ),
            ).rowcount
        return bool(updated)

    def discard_request(self, session: SessionContext, receipt: RequestReceipt) -> None:
        """Release a claim that never reached execution and may be retried."""

        with self._connection() as connection:
            connection.execute(
                f"""
                DELETE FROM {self._request_receipts_table}
                WHERE key_hash = ? AND principal_hash = ? AND conversation_id = ?
                  AND route = ? AND request_id = ? AND state = 'running'
                """,
                (
                    receipt.key_hash,
                    session.principal_hash,
                    receipt.conversation_id,
                    receipt.route,
                receipt.request_id,
            ),
        )

    def find_request_receipt(
        self,
        session: SessionContext,
        conversation_id: str,
        request_id: str,
    ) -> RequestReceipt | None:
        """Return one owned, finished public result by its opaque request id."""

        if not _valid_conversation_id(conversation_id) or not isinstance(request_id, str):
            return None
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT *
                FROM {self._request_receipts_table}
                WHERE principal_hash = ? AND conversation_id = ? AND request_id = ?
                  AND state IN ('completed', 'failed')
                LIMIT 1
                """,
                (session.principal_hash, conversation_id, request_id),
            ).fetchone()
        return self._request_receipt_from_row(row) if row is not None else None

    def create_metric_feedback(
        self,
        session: SessionContext,
        *,
        metric_name: str,
        description: str,
        example_request: str | None = None,
        scope_and_grain: str | None = None,
        unit_and_window: str | None = None,
        notes: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        if conversation_id:
            self.assert_owned(session, conversation_id)
        receipt = "mf_" + secrets.token_urlsafe(18)
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                f"""
                INSERT INTO {self._feedback_table}
                    (receipt_id, principal_hash, conversation_id, metric_name,
                     description, example_request, scope_and_grain, unit_and_window,
                     notes, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')
                """,
                (
                    receipt,
                    session.principal_hash,
                    conversation_id,
                    metric_name,
                    description,
                    example_request,
                    scope_and_grain,
                    unit_and_window,
                    notes,
                    now,
                ),
            )
        return receipt

    def revoke(self, session: SessionContext) -> None:
        """Revoke one browser session without deleting the principal's history."""

        with self._connection() as connection:
            connection.execute(
                f"DELETE FROM {self._leases_table} WHERE principal_hash = ?",
                (session.principal_hash,),
            )
            connection.execute(
                f"DELETE FROM {self._sessions_table} WHERE token_hash = ?",
                (session.token_hash,),
            )

    def health(self) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute(f"SELECT 1 FROM {self._sessions_table} LIMIT 1").fetchone()
            context_count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {self._contexts_table}"
            ).fetchone()["count"]
            feedback_count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {self._feedback_table}"
            ).fetchone()["count"]
        return {
            "ready": True,
            "mode": "principal_bound_sqlite",
            "session_ttl_seconds": self.session_ttl_seconds,
            "max_conversations": self.max_conversations,
            "principal_context_count": int(context_count),
            "feedback_candidate_count": int(feedback_count),
        }


def _valid_cookie(value: str | None) -> bool:
    return isinstance(value, str) and bool(COOKIE_PATTERN.fullmatch(value))


def _valid_conversation_id(value: str | None) -> bool:
    return isinstance(value, str) and bool(CONVERSATION_PATTERN.fullmatch(value))


def _bounded_int(value: int, *, minimum: int, maximum: int) -> int:
    integer = int(value)
    if integer < minimum or integer > maximum:
        raise ValueError(f"value must be between {minimum} and {maximum}")
    return integer
