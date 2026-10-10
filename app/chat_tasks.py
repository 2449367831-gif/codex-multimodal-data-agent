"""Durable, owner-bound receipts for detached chat execution.

Only the screened public response is persisted, never runner output, SQL,
credentials or clarification tokens. A restarted worker marks unfinished work
as interrupted; the browser can explicitly resubmit using the same key.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Collection
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
from uuid import uuid4

from .chat_sessions import ConversationNotFoundError, RequestIdempotencyConflictError
from .errors import RuntimeErrorBase


STAGES = {
    "queued": "等待前一条请求完成",
    "routing": "正在识别本次业务条件",
    "model": "正在分析取数需求",
    "query": "正在查询 Doris",
    "validation": "正在校验并整理结果",
    "rendering": "正在生成数据图片",
    "completed": "处理完成",
    "failed": "本次处理未完成",
    "interrupted": "服务已重启，等待恢复请求",
    "cancelled": "查询已停止",
}
# Age permits recovery only when the service no longer owns a live worker.
# A detached worker must become retryable quickly, while live workers are
# protected by live_task_ids and are never reclaimed by this timeout.
STALE_RUNNING_SECONDS = 5 * 60
PROGRESS: ContextVar[Callable[[str], None] | None] = ContextVar("chat_task_progress", default=None)


def report_progress(stage: str) -> None:
    callback = PROGRESS.get()
    if callback is not None and stage in STAGES:
        callback(stage)


class TaskCapacityError(RuntimeErrorBase):
    code = "task_queue_full"
    http_status = 429


class ChatTaskStore:
    def __init__(self, path: Path, *, live_task_ids: Callable[[], Collection[str]] | None = None) -> None:
        self.path = path
        self.live_task_ids = live_task_ids or (lambda: ())
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_tasks_v1 (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, key_hash TEXT NOT NULL,
                conversation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, stage TEXT NOT NULL, response TEXT,
                http_status INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(owner, key_hash))""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self) -> None:
        with self.connection() as db:
            db.execute("UPDATE chat_tasks_v1 SET state='interrupted', stage='interrupted' WHERE state IN ('queued', 'running')")

    def recover_stale(self, *, now: float | None = None) -> int:
        """Release abandoned tasks without touching active work."""

        current = time.time() if now is None else now
        cutoff = current - STALE_RUNNING_SECONDS
        live_ids = tuple(self.live_task_ids())
        live_filter = (" AND id NOT IN (" + ",".join("?" for _ in live_ids) + ")") if live_ids else ""
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE chat_tasks_v1 SET state='interrupted', stage='interrupted', updated_at=? "
                "WHERE state IN ('queued', 'running') AND updated_at < ?" + live_filter,
                (current, cutoff, *live_ids),
            )
            return cursor.rowcount

    def claim(self, owner: str, conversation_id: str, key: str, fingerprint: str) -> tuple[dict[str, Any], bool]:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        now = time.time()
        self.recover_stale(now=now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM chat_tasks_v1 WHERE owner=? AND key_hash=?", (owner, key_hash)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint or row["conversation_id"] != conversation_id:
                    raise RequestIdempotencyConflictError("请求凭证已被使用")
                # Replaying interrupted work requires a new browser submission.
                if row["state"] != "interrupted":
                    return self.public(row), False
                task_id = row["id"]
                db.execute("UPDATE chat_tasks_v1 SET state='queued', stage='queued', updated_at=? WHERE id=?", (now, task_id))
            else:
                count = db.execute("SELECT COUNT(*) FROM chat_tasks_v1 WHERE state IN ('queued','running') AND owner=?", (owner,)).fetchone()[0]
                total = db.execute("SELECT COUNT(*) FROM chat_tasks_v1 WHERE state IN ('queued','running')").fetchone()[0]
                if count >= 8 or total >= 64:
                    raise TaskCapacityError("待执行请求较多，请稍后再试")
                task_id = "task_" + uuid4().hex
                db.execute("INSERT INTO chat_tasks_v1 VALUES (?,?,?,?,?,'queued','queued',NULL,NULL,?,?)", (task_id, owner, key_hash, conversation_id, fingerprint, now, now))
            db.execute("DELETE FROM chat_tasks_v1 WHERE updated_at < ? AND state NOT IN ('queued','running')", (now - 7 * 86400,))
            row = db.execute("SELECT * FROM chat_tasks_v1 WHERE id=?", (task_id,)).fetchone()
            return self.public(row), True

    def get(self, owner: str, task_id: str) -> dict[str, Any]:
        self.recover_stale()
        with self.connection() as db:
            row = db.execute("SELECT * FROM chat_tasks_v1 WHERE id=? AND owner=?", (task_id, owner)).fetchone()
        if row is None:
            raise ConversationNotFoundError("任务不存在")
        return self.public(row)

    def progress(self, owner: str, task_id: str, stage: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE chat_tasks_v1 SET state='running', stage=?, updated_at=? WHERE id=? AND owner=? AND state IN ('queued','running')", (stage, time.time(), task_id, owner))

    def finish(self, owner: str, task_id: str, payload: dict[str, Any], status: int) -> None:
        outcome = payload.get("outcome")
        business_state = outcome.get("state") if isinstance(outcome, dict) else None
        failed = status >= 400 or payload.get("status") in {"failed", "blocked"} or business_state in {"failed", "blocked"}
        state = "failed" if failed else "completed"
        with self.connection() as db:
            db.execute("UPDATE chat_tasks_v1 SET state=?, stage=?, response=?, http_status=?, updated_at=? WHERE id=? AND owner=? AND state IN ('queued','running')", (state, state, json.dumps(payload, ensure_ascii=False), status, time.time(), task_id, owner))

    def interrupt(self, owner: str, task_id: str) -> None:
        """Close a worker that exited before it could persist a response."""

        with self.connection() as db:
            db.execute(
                "UPDATE chat_tasks_v1 SET state='interrupted', stage='interrupted', "
                "updated_at=? WHERE id=? AND owner=? AND state IN ('queued','running')",
                (time.time(), task_id, owner),
            )

    def cancel(self, owner: str, task_id: str) -> dict[str, Any]:
        """Cancel an active task and fence off any late worker write-back."""

        now = time.time()
        payload = {
            "status": "cancelled",
            "outcome": {
                "state": "cancelled",
                "label": "查询已停止",
            },
            "error": {
                "code": "query_cancelled",
                "message": "查询已由用户停止",
                "details": {},
            },
        }
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM chat_tasks_v1 WHERE id=? AND owner=?",
                (task_id, owner),
            ).fetchone()
            if row is None:
                raise ConversationNotFoundError("任务不存在")
            if row["state"] in {"queued", "running"}:
                db.execute(
                    "UPDATE chat_tasks_v1 SET state='cancelled', stage='cancelled', "
                    "response=?, http_status=499, updated_at=? "
                    "WHERE id=? AND owner=? AND state IN ('queued','running')",
                    (json.dumps(payload, ensure_ascii=False), now, task_id, owner),
                )
            row = db.execute(
                "SELECT * FROM chat_tasks_v1 WHERE id=? AND owner=?",
                (task_id, owner),
            ).fetchone()
        return self.public(row)

    @staticmethod
    def public(row: sqlite3.Row) -> dict[str, Any]:
        result = {"task_id": row["id"], "conversation_id": row["conversation_id"], "state": row["state"], "stage": row["stage"], "stage_label": STAGES.get(row["stage"], "正在处理"), "created_at": row["created_at"], "updated_at": row["updated_at"]}
        if row["response"] is not None:
            result["result"] = json.loads(row["response"])
            result["http_status"] = row["http_status"]
        return result
