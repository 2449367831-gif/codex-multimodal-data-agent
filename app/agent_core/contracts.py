"""Small, transport-independent contracts for the query agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol


class SemanticExecutor(Protocol):
    def classify(self, question: str) -> Any | None: ...

    def run_if_supported(
        self, question: str, *, context_reused: bool = False
    ) -> dict[str, Any] | None: ...

    def run_selected_if_supported(
        self,
        source_question: str | None,
        selected_answers: list[dict[str, Any]] | None,
        *,
        context_reused: bool = False,
        reference_time: datetime | None = None,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class QueryIntent:
    """A reviewed route selected by the semantic layer."""

    route: str
    start: str | None
    end: str | None
    grain: str | None
    scope: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CoreResult:
    payload: dict[str, Any]
    intent: QueryIntent
