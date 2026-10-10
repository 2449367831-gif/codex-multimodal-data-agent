"""Bounded request memory shared by deterministic and model query paths."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from typing import Any

from .learning import _safe_text


class RequestContextStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_request_context_v1 (
                owner TEXT NOT NULL, request_id TEXT NOT NULL, question TEXT NOT NULL,
                plan TEXT NOT NULL, outcome TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(owner, request_id))""")

    def record(self, owner: str, request_id: str, question: str, plan: Any, outcome: str) -> None:
        safe_question = _safe_text(question, maximum=800)
        # Scope/date metadata only. No answers, SQL or database rows enter memory.
        safe_plan = {key: plan[key] for key in ("route", "start", "end", "production_date", "scope", "grain", "top_n") if isinstance(plan, dict) and key in plan}
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("INSERT OR IGNORE INTO chat_request_context_v1 VALUES (?,?,?,?,?,?)", (owner, request_id, safe_question, json.dumps(safe_plan, ensure_ascii=False), outcome, time.time()))
            db.execute("DELETE FROM chat_request_context_v1 WHERE owner=? AND request_id NOT IN (SELECT request_id FROM chat_request_context_v1 WHERE owner=? ORDER BY created_at DESC LIMIT 100)", (owner, owner))

    def recent(self, owner: str, limit: int = 6) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute("SELECT question, plan, outcome FROM chat_request_context_v1 WHERE owner=? ORDER BY created_at DESC LIMIT ?", (owner, min(max(limit, 1), 20))).fetchall()
        return [{"question": q, "plan": json.loads(p), "outcome": o} for q, p, o in reversed(rows)]
