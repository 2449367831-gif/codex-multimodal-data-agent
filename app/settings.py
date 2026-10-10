from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return tuple(
        item.strip().casefold()
        for item in value.split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class RuntimeSettings:
    project_root: Path
    executor_mode: str = "mock"
    audit_db_path: Path | None = None
    mock_fixture_path: Path | None = None
    doris_host: str | None = None
    doris_port: int = 9030
    doris_user: str | None = None
    doris_password: str | None = None
    doris_database: str | None = None
    query_timeout_seconds: int = 180
    max_rows: int = 100000
    max_sql_length: int = 100000
    max_response_bytes: int = 64 * 1024 * 1024
    sql_allowed_schemas: tuple[str, ...] = (
        "ywzt_ods",
        "dms_ods",
        "information_schema",
    )
    sql_allowed_catalogs: tuple[str, ...] = ("internal",)
    allow_unapproved_contracts: bool = False
    release_id: str | None = None
    trusted_release_sha256: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.doris_port <= 65535:
            raise ValueError("DORIS_PORT must be between 1 and 65535")
        if not 1 <= self.query_timeout_seconds <= 180:
            raise ValueError(
                "AGENT_QUERY_TIMEOUT_SECONDS must be between 1 and the 180-second source-policy cap"
            )
        if not 1 <= self.max_rows <= 100000:
            raise ValueError(
                "AGENT_MAX_ROWS must be between 1 and the 100000-row hard cap"
            )
        if not 1 <= self.max_sql_length <= 100000:
            raise ValueError(
                "AGENT_MAX_SQL_LENGTH must be between 1 and the 100000-character hard cap"
            )
        if not 1024 <= self.max_response_bytes <= 64 * 1024 * 1024:
            raise ValueError(
                "AGENT_MAX_RESPONSE_BYTES must be between 1024 and the 67108864-byte hard cap"
            )

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "RuntimeSettings":
        configured_root = (os.getenv("AGENT_PROJECT_ROOT") or "").strip()
        root = (
            project_root
            or (Path(configured_root).expanduser() if configured_root else None)
            or Path(__file__).resolve().parents[1]
        ).resolve()
        audit_value = os.getenv("AGENT_AUDIT_DB")
        fixture_value = os.getenv("AGENT_MOCK_FIXTURES")
        return cls(
            project_root=root,
            executor_mode=os.getenv("DORIS_EXECUTION_MODE", "mock").strip().lower(),
            audit_db_path=(
                Path(audit_value).resolve()
                if audit_value
                else root / "var" / "audit.sqlite3"
            ),
            mock_fixture_path=Path(fixture_value).resolve() if fixture_value else None,
            doris_host=os.getenv("DORIS_HOST"),
            doris_port=_env_int("DORIS_PORT", 9030),
            doris_user=os.getenv("DORIS_USER"),
            doris_password=os.getenv("DORIS_PASSWORD"),
            doris_database=os.getenv("DORIS_DATABASE"),
            query_timeout_seconds=_env_int("AGENT_QUERY_TIMEOUT_SECONDS", 180),
            max_rows=_env_int("AGENT_MAX_ROWS", 100000),
            max_sql_length=_env_int("AGENT_MAX_SQL_LENGTH", 100000),
            max_response_bytes=_env_int("AGENT_MAX_RESPONSE_BYTES", 64 * 1024 * 1024),
            sql_allowed_schemas=_env_csv(
                "AGENT_SQL_ALLOWED_SCHEMAS",
                ("ywzt_ods", "dms_ods", "information_schema"),
            ),
            sql_allowed_catalogs=_env_csv(
                "AGENT_SQL_ALLOWED_CATALOGS",
                ("internal",),
            ),
            allow_unapproved_contracts=_env_bool("AGENT_ALLOW_UNAPPROVED_CONTRACTS", False),
            release_id=(os.getenv("AGENT_RELEASE_ID") or "").strip() or None,
            trusted_release_sha256=(
                (os.getenv("AGENT_TRUSTED_RELEASE_SHA256") or "").strip().casefold() or None
            ),
        )
