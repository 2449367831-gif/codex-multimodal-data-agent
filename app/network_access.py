"""Persistent LAN access requests and approvals for the local admin surface."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
from pathlib import Path
import sqlite3
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_private_ip(value: str) -> str:
    address = ipaddress.ip_address(str(value).strip())
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("loopback or special address")
    if not address.is_private:
        raise ValueError("public address")
    return str(address)


class NetworkAccessRegistry:
    """Store requests in the installation SQLite database.

    The registry stores only the client IP, a bounded applicant note, and
    bounded timestamps. It does not collect browser headers, credentials, or
    query content.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS network_access_requests (
                    ip TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                    note TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 1,
                    approved_at TEXT,
                    rejected_at TEXT
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(network_access_requests)").fetchall()}
            if "note" not in columns:
                connection.execute("ALTER TABLE network_access_requests ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            connection.commit()

    def record_request(self, value: str, note: str | None = None) -> dict[str, Any] | None:
        try:
            ip = normalize_private_ip(value)
        except ValueError:
            return None
        bounded_note = str(note or "").strip()[:240]
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO network_access_requests(ip, status, note, first_seen, last_seen, hit_count)
                VALUES (?, 'pending', ?, ?, ?, 1)
                ON CONFLICT(ip) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    hit_count=network_access_requests.hit_count + 1,
                    note=CASE WHEN excluded.note <> '' THEN excluded.note ELSE network_access_requests.note END,
                    status=CASE
                        WHEN network_access_requests.status = 'approved' THEN 'approved'
                        ELSE 'pending'
                    END
                """,
                (ip, bounded_note, now, now),
            )
            connection.commit()
        return self.get(ip)

    def is_approved(self, value: str) -> bool:
        try:
            ip = normalize_private_ip(value)
        except ValueError:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM network_access_requests WHERE ip = ?", (ip,)
            ).fetchone()
        return bool(row and row["status"] == "approved")

    def get(self, value: str) -> dict[str, Any] | None:
        try:
            ip = normalize_private_ip(value)
        except ValueError:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT ip, status, note, first_seen, last_seen, hit_count, approved_at, rejected_at
                FROM network_access_requests WHERE ip = ?
                """,
                (ip,),
            ).fetchone()
        return dict(row) if row else None

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT ip, status, note, first_seen, last_seen, hit_count, approved_at, rejected_at "
            "FROM network_access_requests"
        )
        params: tuple[str, ...] = ()
        if status in {"pending", "approved", "rejected"}:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, last_seen DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def approve(self, value: str) -> dict[str, Any]:
        ip = normalize_private_ip(value)
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO network_access_requests(ip, status, note, first_seen, last_seen, hit_count, approved_at)
                VALUES (?, 'approved', '', ?, ?, 0, ?)
                ON CONFLICT(ip) DO UPDATE SET status='approved', approved_at=?, rejected_at=NULL
                """,
                (ip, now, now, now, now),
            )
            connection.commit()
        return self.get(ip) or {"ip": ip, "status": "approved"}

    def reject(self, value: str) -> dict[str, Any]:
        ip = normalize_private_ip(value)
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO network_access_requests(ip, status, note, first_seen, last_seen, hit_count, rejected_at)
                VALUES (?, 'rejected', '', ?, ?, 0, ?)
                ON CONFLICT(ip) DO UPDATE SET status='rejected', rejected_at=?
                """,
                (ip, now, now, now, now),
            )
            connection.commit()
        return self.get(ip) or {"ip": ip, "status": "rejected"}
