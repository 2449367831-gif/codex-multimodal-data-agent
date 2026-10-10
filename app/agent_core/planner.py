"""Planner boundary for deterministic, reviewed business intents."""

from __future__ import annotations

from typing import Any

from .contracts import QueryIntent, SemanticExecutor


def intent_from_request(request: Any) -> QueryIntent:
    scope = request.scope.as_dict() if hasattr(request.scope, "as_dict") else {}
    grain = "sku" if getattr(request, "show_top_sku", False) else (
        "warehouse" if getattr(request, "show_warehouses", False) else "total"
    )
    if getattr(request, "sales_aggregation", None):
        grain = "+".join(request.sales_aggregation.dimensions) or "total"
    if getattr(request, "dms_order_plan", None):
        grain = "month+order"
    if getattr(request, "so07_plan", None):
        grain = "owner+order+detail"
    if getattr(request, "outbound_link_plan", None):
        grain = "owner+outbound+detail_source"
    if getattr(request, "order_return_plan", None):
        grain = "owner+order+sku"
        scope = request.order_return_plan.scope_metadata()
    return QueryIntent(
        route=str(request.route),
        start=request.start.isoformat() if request.start else None,
        end=request.end.isoformat() if request.end else None,
        grain=grain,
        scope=scope,
    )


class ReviewedPlanner:
    """Expose only the semantic executor's reviewed classification."""

    def __init__(self, executor: SemanticExecutor) -> None:
        self.executor = executor

    def classify(self, question: str) -> Any | None:
        classifier = getattr(self.executor, "classify", None)
        return classifier(question) if callable(classifier) else None

    def intent(self, question: str) -> QueryIntent | None:
        request = self.classify(question)
        return intent_from_request(request) if request is not None else None
