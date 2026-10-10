"""Core lifecycle for the single-purpose Doris query agent.

The model is allowed to handle requests that are outside the reviewed
deterministic routes, but reviewed routes never depend on a second model turn.
That separation prevents a valid question from becoming a clarification loop
or a model timeout after the business definition is already known.
"""

from __future__ import annotations

from typing import Any
from datetime import datetime
import inspect

from pathlib import Path

from .capsule import ExecutionCapsule
from .catalog import ReleasedMetricCatalog
from .contracts import QueryIntent, SemanticExecutor
from .planner import ReviewedPlanner, intent_from_request
from .generic_sales_composition import sales_composition_request


class CoreExecutionError(RuntimeError):
    """A deterministic route returned an incomplete execution envelope."""


class AgentOrchestrator:
    """One stable entry point for all reviewed semantic query execution."""

    VERSION = "4.0"

    def __init__(self, executor: SemanticExecutor, *, project_root: Path | None = None) -> None:
        self.executor = executor
        self.planner = ReviewedPlanner(executor)
        self.catalog = (
            ReleasedMetricCatalog(project_root)
            if project_root is not None
            else None
        )

    def _validate_result(self, result: Any, intent: Any, question: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise CoreExecutionError("semantic executor returned an invalid result")
        evidence = result.get("query_execution")
        if not isinstance(evidence, dict):
            raise CoreExecutionError("semantic executor returned no execution evidence")
        if not evidence.get("live_business_result_executed"):
            raise CoreExecutionError("semantic executor returned no live business result")
        plan = result.get("query_plan")
        if not isinstance(plan, dict) or plan.get("route") != intent.route:
            raise CoreExecutionError("semantic executor returned a mismatched query plan")
        capsule = ExecutionCapsule.create(question, intent)
        result["agent_core"] = {
            "version": AgentOrchestrator.VERSION,
            "reviewed": True,
            "kernel": "unified-semantic-query",
            "route": capsule.route,
            "scope": capsule.scope.as_dict(),
        }
        if self.catalog is not None:
            metric = self.catalog.for_route(intent.route, scope=intent.scope)
            if metric is not None:
                result["query_plan"]["metric_id"] = metric.metric_id
                result["query_plan"]["metric_version"] = metric.version
                result["query_plan"]["release_digest"] = self.catalog.release_digest
        return result

    def execute_plan(
        self, request: Any, *, question: str, context_reused: bool = False
    ) -> dict[str, Any]:
        """Execute an already-bound plan without reclassifying its wording.

        Explicit rule forms arrive here with dates and scope already resolved.
        Re-running the natural-language planner would make a rule submission
        vulnerable to date-boundary drift, stale aliases, or a model rewrite.
        """
        if self.catalog is not None:
            metric = self.catalog.for_route(request.route, scope=request.scope.as_dict())
            if metric is None:
                raise CoreExecutionError("compiled rule is not backed by an approved metric")
        execute_plan = getattr(self.executor, "execute_plan", None)
        if not callable(execute_plan):
            raise CoreExecutionError("compiled rule executor is not available")
        result = execute_plan(request, question=question, context_reused=context_reused)
        if result is None:
            raise CoreExecutionError("compiled rule returned no result")
        return self._validate_result(result, intent_from_request(request), question)

    def run_if_supported(
        self, question: str, *, context_reused: bool = False
    ) -> dict[str, Any] | None:
        request = self.planner.classify(question)
        if request is None:
            composition = sales_composition_request(
                question,
                planner=getattr(self.executor, "planner", self.planner),
            )
            if composition is not None:
                result = self.executor.execute_plan(
                    composition, question=question, context_reused=context_reused
                )
                return self._validate_result(result, intent_from_request(composition), question)
            # Compatibility executors used by tests and downstream embedders may
            # already own their reviewed routing and expose no classifier.
            if callable(getattr(self.executor, "classify", None)):
                return None
            result = self.executor.run_if_supported(
                question, context_reused=context_reused
            )
            return result
        intent = intent_from_request(request)
        execute_plan = getattr(self.executor, "execute_plan", None)
        if callable(execute_plan):
            # Execute the exact plan we validate. Reclassification can cross a
            # calendar boundary or observe a different semantic release.
            result = execute_plan(request, question=question, context_reused=context_reused)
        else:
            result = self.executor.run_if_supported(
                question, context_reused=context_reused
            )
        if result is None:
            raise CoreExecutionError("reviewed intent was not executable")
        return self._validate_result(result, intent, question)

    def run_selected_if_supported(
        self,
        source_question: str | None,
        selected_answers: list[dict[str, Any]] | None,
        *,
        context_reused: bool = False,
        reference_time: datetime | None = None,
    ) -> dict[str, Any] | None:
        if not source_question:
            return None
        bound_time = {"reference_time": reference_time} if reference_time is not None else {}
        plan_selected = getattr(self.executor, "plan_selected", None)
        execute_plan = getattr(self.executor, "execute_plan", None)
        if callable(plan_selected) and callable(execute_plan):
            request = plan_selected(source_question, selected_answers, **bound_time)
            if request is None:
                return None
            result = execute_plan(request, question=source_question, context_reused=context_reused)
            return self._validate_result(result, intent_from_request(request), source_question)
        if bound_time:
            try:
                inspect.signature(self.executor.run_selected_if_supported).bind(
                    source_question, selected_answers, context_reused=context_reused, **bound_time
                )
            except (TypeError, ValueError):
                # A legacy executor must not silently reinterpret the original date.
                return None
        result = self.executor.run_selected_if_supported(
            source_question,
            selected_answers,
            context_reused=context_reused,
            **bound_time,
        )
        if result is None:
            return None
        plan = result.get("query_plan") if isinstance(result, dict) else None
        if not isinstance(plan, dict):
            raise CoreExecutionError("selected route returned no query plan")
        # Compatibility executors own their selected plan. Never reclassify
        # the unselected question and overwrite the user's confirmed variant.
        intent = QueryIntent(
            route=str(plan.get("route") or ""),
            start=plan.get("start"),
            end=plan.get("end"),
            grain=plan.get("grain"),
            scope=plan.get("scope") if isinstance(plan.get("scope"), dict) else {},
        )
        return self._validate_result(result, intent, source_question)
