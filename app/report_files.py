"""Owner-bound storage for generated report files."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any


class ReportFileNotFound(FileNotFoundError):
    pass


@dataclass(frozen=True, slots=True)
class ReportFileRecord:
    file_id: str
    path: Path
    principal_hash: str
    conversation_id: str
    report_id: str
    format: str
    filename: str
    content_type: str
    created_at: float
    expires_at: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "report_id": self.report_id,
            "format": self.format,
            "filename": self.filename,
            "content_type": self.content_type,
            "url": f"/v1/reports/files/{self.file_id}",
            "expires_at": int(self.expires_at),
        }


class ReportFileStore:
    def __init__(self, root: Path, *, ttl_seconds: int = 2 * 60 * 60, max_bytes: int = 30_000_000, clock=time.time) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = int(ttl_seconds)
        self.max_bytes = int(max_bytes)
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, ReportFileRecord] = {}

    def _purge_locked(self, now: float) -> None:
        for file_id, record in list(self._records.items()):
            if record.expires_at <= now:
                self._records.pop(file_id, None)
                try:
                    record.path.unlink(missing_ok=True)
                except OSError:
                    pass

    def register(self, content: bytes, *, principal_hash: str, conversation_id: str, report_id: str, format: str, filename: str, content_type: str) -> dict[str, Any]:
        if not isinstance(content, (bytes, bytearray)) or not content or len(content) > self.max_bytes:
            raise ValueError("report file is empty or too large")
        now = float(self._clock())
        file_id = "rpf_" + secrets.token_urlsafe(24)
        target = (self.root / f"{file_id}.bin").resolve()
        target.relative_to(self.root)
        temp = self.root / f".{file_id}.{secrets.token_hex(8)}.tmp"
        try:
            temp.write_bytes(bytes(content))
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        record = ReportFileRecord(file_id, target, str(principal_hash), str(conversation_id), str(report_id), str(format), str(filename), str(content_type), now, now + self.ttl_seconds)
        with self._lock:
            self._purge_locked(now)
            self._records[file_id] = record
        return record.public_dict()

    def resolve(self, file_id: str, *, principal_hash: str, conversation_id: str) -> ReportFileRecord:
        if not isinstance(file_id, str) or not file_id.startswith("rpf_"):
            raise ReportFileNotFound("report file not found")
        with self._lock:
            self._purge_locked(float(self._clock()))
            record = self._records.get(file_id)
            if record is None or not hmac.compare_digest(record.principal_hash, str(principal_hash)) or not hmac.compare_digest(record.conversation_id, str(conversation_id)):
                raise ReportFileNotFound("report file not found")
            if not record.path.is_file():
                raise ReportFileNotFound("report file not found")
            return record


__all__ = ["ReportFileNotFound", "ReportFileRecord", "ReportFileStore"]
