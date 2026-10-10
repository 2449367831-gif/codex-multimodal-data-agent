from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .serialization import to_json_value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class RouteCandidate:
    metric_id: str
    version: str
    score: float
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return to_json_value(self)


@dataclass(frozen=True)
class RouteDecision:
    question: str
    selected_metric_id: str | None
    candidates: tuple[RouteCandidate, ...]
    needs_clarification: bool
    clarification_question: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return to_json_value(self)


@dataclass(frozen=True)
class QueryPlan:
    request_id: str
    metric_id: str
    metric_version: str
    question: str | None
    parameters: dict[str, Any]
    sql: str
    bindings: dict[str, Any]
    validation_results: tuple[dict[str, Any], ...]
    source_tables: tuple[str, ...]
    executor_name: str
    # Optional for backwards compatibility with callers constructing plans directly.
    release_id: str | None = None
    source_versions: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def as_dict(self, *, include_sql: bool = True) -> dict[str, Any]:
        value = to_json_value(self)
        if not include_sql:
            value.pop("sql", None)
        return value


@dataclass(frozen=True)
class ExecutionResult:
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    row_count: int
    truncated: bool
    duration_ms: int
    executor_name: str
    is_mock: bool = False
    truncation_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return to_json_value(self)
