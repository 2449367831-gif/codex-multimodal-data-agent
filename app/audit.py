from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from .serialization import to_json_value


SCHEMA = """
CREATE TABLE IF NOT EXISTS query_audit (
    request_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    metric_id TEXT,
    metric_version TEXT,
    release_id TEXT,
    source_versions_json TEXT NOT NULL DEFAULT '{}',
    question TEXT,
    parameters_json TEXT NOT NULL,
    sql_text TEXT,
    bindings_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    executor_name TEXT,
    duration_ms INTEGER,
    row_count INTEGER,
    truncated INTEGER,
    is_mock INTEGER,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_query_audit_created_at ON query_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_query_audit_metric_id ON query_audit(metric_id);
"""


JSON_COLUMNS = {
    "parameters_json",
    "bindings_json",
    "validation_json",
    "source_versions_json",
    "error_json",
}

MIGRATION_COLUMNS = {
    "release_id": "TEXT",
    "source_versions_json": "TEXT NOT NULL DEFAULT '{}'",
}


class AuditStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if path is None:
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
            self._ensure_schema(self._memory_connection)

    def close(self) -> None:
        with self._lock:
            if self._memory_connection is not None:
                self._memory_connection.close()
                self._memory_connection = None

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA)
        existing = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(query_audit)").fetchall()
        }
        for name, declaration in MIGRATION_COLUMNS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE query_audit ADD COLUMN {name} {declaration}")

    def _connect(self) -> sqlite3.Connection:
        if self.path is None:
            assert self._memory_connection is not None
            return self._memory_connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        self._ensure_schema(connection)
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            if self.path is not None:
                connection.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(to_json_value(value), ensure_ascii=False, separators=(",", ":"))

    def upsert(self, record: dict[str, Any]) -> None:
        fields = {
            "request_id": record["request_id"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "status": record["status"],
            "metric_id": record.get("metric_id"),
            "metric_version": record.get("metric_version"),
            "release_id": record.get("release_id"),
            "source_versions_json": self._json(record.get("source_versions", {})),
            "question": record.get("question"),
            "parameters_json": self._json(record.get("parameters", {})),
            "sql_text": record.get("sql"),
            "bindings_json": self._json(record.get("bindings", {})),
            "validation_json": self._json(record.get("validation_results", [])),
            "executor_name": record.get("executor_name"),
            "duration_ms": record.get("duration_ms"),
            "row_count": record.get("row_count"),
            "truncated": int(record["truncated"]) if record.get("truncated") is not None else None,
            "is_mock": int(record["is_mock"]) if record.get("is_mock") is not None else None,
            "error_json": self._json(record["error"]) if record.get("error") is not None else None,
        }
        columns = list(fields)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "request_id")
        sql = (
            f"INSERT INTO query_audit ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(request_id) DO UPDATE SET {updates}"
        )
        with self._lock, self._connection() as connection:
            connection.execute(sql, [fields[column] for column in columns])

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM query_audit WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def list(self, *, limit: int = 50, metric_id: str | None = None) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        if metric_id:
            sql = "SELECT * FROM query_audit WHERE metric_id = ? ORDER BY created_at DESC LIMIT ?"
            values: tuple[Any, ...] = (metric_id, bounded_limit)
        else:
            sql = "SELECT * FROM query_audit ORDER BY created_at DESC LIMIT ?"
            values = (bounded_limit,)
        with self._lock, self._connection() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for column in JSON_COLUMNS:
            raw = value.pop(column)
            public_name = column.removesuffix("_json")
            value[public_name] = json.loads(raw) if raw is not None else None
        for name in ("truncated", "is_mock"):
            if value[name] is not None:
                value[name] = bool(value[name])
        return value
