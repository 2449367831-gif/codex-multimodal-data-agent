"""Stable domain contracts for the unified Doris query kernel.

This module deliberately contains no database client. It gives the model
planner, deterministic accelerators, and the public response layer one shared
shape for scope, plans, execution evidence, reconciliation, and outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

import sqlglot
from sqlglot import exp


class QueryOutcome(StrEnum):
    CLARIFICATION_REQUIRED = "clarification_required"
    BACKEND_REJECTED = "backend_rejected"
    QUERY_FAILED = "query_failed"
    QUERY_TIMEOUT = "query_timeout"
    RESULT_TRUNCATED = "result_truncated"
    NO_DATA = "no_data"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class QueryRequest:
    question: str
    request_id: str
    timezone: str = "Asia/Shanghai"
    owner_scope: str | None = None
    sku_scope: tuple[str, ...] = ()
    warehouse_scope: tuple[str, ...] = ()
    channel_scope: tuple[str, ...] = ()
    unit: str | None = None
    grain: str | None = None
    start_inclusive: datetime | None = None
    end_exclusive: datetime | None = None

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("question must not be empty")
        if self.start_inclusive and self.end_exclusive and self.start_inclusive >= self.end_exclusive:
            raise ValueError("query window must be a non-empty half-open interval")

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        if self.start_inclusive is None or self.end_exclusive is None:
            return None
        return self.start_inclusive, self.end_exclusive


@dataclass(frozen=True, slots=True)
class SemanticContext:
    release_id: str | None
    metric_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    contract_versions: Mapping[str, str] = field(default_factory=dict)
    unresolved_decisions: tuple[str, ...] = ()
    scope_defaults: str = "ALL"

    @property
    def executable(self) -> bool:
        return bool(self.release_id) and not self.unresolved_decisions


@dataclass(frozen=True, slots=True)
class QueryPlan:
    request_id: str
    sql: str
    purpose: str
    semantic: SemanticContext | None = None
    source_tables: tuple[str, ...] = ()
    bindings: Mapping[str, Any] = field(default_factory=dict)

    def validate_private(self) -> None:
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ValueError("query plan must contain a query")
        if len(self.sql) > 100_000:
            raise ValueError("query plan exceeds SQL length limit")
        if not isinstance(self.purpose, str) or not self.purpose.strip():
            raise ValueError("query plan purpose must not be empty")
        try:
            statements = [node for node in sqlglot.parse(self.sql, read="doris")
                          if node is not None and not (isinstance(node, exp.Semicolon) and not node.args)]
        except sqlglot.errors.SqlglotError:
            raise ValueError("query plan is not valid Doris syntax") from None
        if len(statements) != 1:
            raise ValueError("query plan must contain one statement")
        root = statements[0]
        if not isinstance(root, exp.Query) or root.find(exp.Select) is None:
            raise ValueError("query plan must be SELECT or WITH ... SELECT")
        if any(isinstance(node, (exp.DDL, exp.DML, exp.Command, exp.Into, exp.Lock))
               for node in root.walk()):
            raise ValueError("query plan must be read-only")
        # This is structural validation only. The execution backend still owns
        # schema/catalog permissions, function restrictions and result limits.


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    outcome: QueryOutcome
    executor: str
    live_doris: bool
    columns: tuple[str, ...] = ()
    rows: tuple[Mapping[str, Any], ...] = ()
    row_count: int = 0
    truncated: bool = False
    duration_ms: int | None = None
    attempts: int = 1

    def validate(self) -> None:
        if self.executor != "doris" or not self.live_doris:
            raise ValueError("business results require live Doris evidence")
        if self.truncated:
            raise ValueError("truncated results cannot be presented as complete")
        if self.row_count != len(self.rows):
            raise ValueError("row_count does not match returned rows")


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    passed: bool
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicAnswer:
    outcome: QueryOutcome
    answer: str | None = None
    table: Mapping[str, Any] | None = None
    unit: str | None = None
    window: str | None = None
    grain: str | None = None
    reconciliation: ReconciliationReport | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"outcome": self.outcome.value, "questions": []}
        if self.answer is not None:
            value["answer"] = self.answer
        if self.table is not None:
            value["business_table"] = dict(self.table)
        for key, item in (("unit", self.unit), ("window", self.window), ("grain", self.grain)):
            if item is not None:
                value[key] = item
        return value
