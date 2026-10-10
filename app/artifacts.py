"""Owner-bound, short-lived report artifacts.

Artifacts are deliberately separate from chat responses.  The browser receives
only an opaque id and a same-origin URL; the file path and report internals stay
inside the service process.  Every read is checked against both the verified
principal and the canonical conversation before a file is served.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any


class ArtifactNotFound(FileNotFoundError):
    """Raised for unknown, expired, or foreign artifacts.

    Callers should map every instance to the same public 404 response.  This
    avoids turning the endpoint into an existence oracle.
    """


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    path: Path
    principal_hash: str
    conversation_id: str
    request_id: str
    kind: str
    title: str
    period: str
    created_at: float
    expires_at: float
    content_type: str = "image/png"

    def public_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "title": self.title,
            "period": self.period,
            "content_type": self.content_type,
            "url": f"/v1/artifacts/{self.artifact_id}",
            "expires_at": int(self.expires_at),
        }


class ArtifactStore:
    """Store bounded PNG files with principal/conversation ownership."""

    _ID_PREFIX = "art_"

    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: int = 2 * 60 * 60,
        max_bytes: int = 12_000_000,
        clock: Any = time.time,
    ) -> None:
        if ttl_seconds < 60 or ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("artifact ttl must be between 60 seconds and 7 days")
        if max_bytes < 1024 or max_bytes > 100_000_000:
            raise ValueError("artifact max_bytes is outside the supported range")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = int(ttl_seconds)
        self.max_bytes = int(max_bytes)
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, ArtifactRecord] = {}

    @staticmethod
    def _bounded_text(value: Any, name: str, maximum: int) -> str:
        text = str(value or "").strip()
        if not text or len(text) > maximum:
            raise ValueError(f"{name} is empty or too long")
        return text

    @classmethod
    def _valid_id(cls, value: str) -> bool:
        return bool(value.startswith(cls._ID_PREFIX) and len(value) >= 24 and all(
            character.isalnum() or character in "_-" for character in value
        ))

    def _purge_locked(self, now: float) -> None:
        expired = [
            artifact_id
            for artifact_id, record in self._records.items()
            if record.expires_at <= now
        ]
        for artifact_id in expired:
            record = self._records.pop(artifact_id, None)
            if record is not None:
                try:
                    record.path.unlink(missing_ok=True)
                except OSError:
                    pass

    def purge(self) -> int:
        with self._lock:
            before = len(self._records)
            self._purge_locked(float(self._clock()))
            return before - len(self._records)

    def register_bytes(
        self,
        content: bytes,
        *,
        principal_hash: str,
        conversation_id: str,
        request_id: str,
        kind: str,
        title: str,
        period: str,
        content_type: str = "image/png",
    ) -> dict[str, Any]:
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise ValueError("artifact content must be non-empty bytes")
        if len(content) > self.max_bytes:
            raise ValueError("artifact exceeds the configured size limit")
        principal = self._bounded_text(principal_hash, "principal_hash", 256)
        conversation = self._bounded_text(conversation_id, "conversation_id", 128)
        request = self._bounded_text(request_id, "request_id", 128)
        artifact_kind = self._bounded_text(kind, "kind", 96)
        artifact_title = self._bounded_text(title, "title", 240)
        artifact_period = self._bounded_text(period, "period", 160)
        if content_type != "image/png":
            raise ValueError("only image/png artifacts are supported")

        now = float(self._clock())
        artifact_id = self._ID_PREFIX + secrets.token_urlsafe(24)
        # A temporary file plus atomic replace prevents a request from ever
        # serving a partially written image.
        final_path = (self.root / f"{artifact_id}.png").resolve()
        try:
            final_path.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - generated ids are safe
            raise ValueError("artifact path escaped root") from exc
        temp_path = self.root / f".{artifact_id}.{secrets.token_hex(8)}.tmp"
        try:
            temp_path.write_bytes(bytes(content))
            os.replace(temp_path, final_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

        record = ArtifactRecord(
            artifact_id=artifact_id,
            path=final_path,
            principal_hash=principal,
            conversation_id=conversation,
            request_id=request,
            kind=artifact_kind,
            title=artifact_title,
            period=artifact_period,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            content_type=content_type,
        )
        with self._lock:
            self._purge_locked(now)
            self._records[artifact_id] = record
        return record.public_dict()

    def resolve(
        self,
        artifact_id: str,
        *,
        principal_hash: str,
        conversation_id: str,
    ) -> ArtifactRecord:
        if not isinstance(artifact_id, str) or not self._valid_id(artifact_id):
            raise ArtifactNotFound("artifact not found")
        with self._lock:
            now = float(self._clock())
            self._purge_locked(now)
            record = self._records.get(artifact_id)
            if record is None:
                raise ArtifactNotFound("artifact not found")
            if not hmac.compare_digest(record.principal_hash, str(principal_hash)):
                raise ArtifactNotFound("artifact not found")
            if not hmac.compare_digest(record.conversation_id, str(conversation_id)):
                raise ArtifactNotFound("artifact not found")
            path = record.path.resolve()
            try:
                path.relative_to(self.root)
            except ValueError as exc:
                raise ArtifactNotFound("artifact not found") from exc
            if path.suffix.casefold() != ".png" or not path.is_file():
                raise ArtifactNotFound("artifact not found")
            try:
                if path.stat().st_size > self.max_bytes:
                    raise ArtifactNotFound("artifact not found")
            except OSError as exc:
                raise ArtifactNotFound("artifact not found") from exc
            return record

    def delete_for_request(self, request_id: str) -> int:
        """Remove artifacts generated by one request (used on failed retries)."""

        removed = 0
        with self._lock:
            for artifact_id, record in list(self._records.items()):
                if hmac.compare_digest(record.request_id, str(request_id)):
                    self._records.pop(artifact_id, None)
                    try:
                        record.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    removed += 1
        return removed


__all__ = ["ArtifactNotFound", "ArtifactRecord", "ArtifactStore"]
