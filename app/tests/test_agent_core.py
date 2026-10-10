from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date, datetime

from app.agent_core import AgentOrchestrator, CoreExecutionError


@dataclass(frozen=True)
class FakeRequest:
    route: str = "sales_outbound_by_warehouse"
    start: date = date(2026, 9, 1)
    end: date = date(2026, 9, 2)
    show_top_sku: bool = False
    show_warehouses: bool = True

    class Scope:
        @staticmethod
        def as_dict() -> dict[str, object]:
            return {"owner": "ALL", "warehouse_codes": ()}

    scope = Scope()


class FakeExecutor:
    def __init__(self, *, live: bool = True) -> None:
        self.live = live
        self.calls: list[str] = []

    def classify(self, question: str) -> FakeRequest | None:
        return FakeRequest() if question else None

    def run_if_supported(self, question: str, *, context_reused: bool = False):
        self.calls.append(question)
        return {
            "query_plan": {"route": "sales_outbound_by_warehouse"},
            "query_execution": {"live_business_result_executed": self.live},
        }

    def run_selected_if_supported(self, source_question, selected_answers, *, context_reused=False):
        return self.run_if_supported(source_question or "selected")


class AgentCoreTestCase(unittest.TestCase):
    def test_reviewed_route_is_executed_through_one_core_entrypoint(self) -> None:
        executor = FakeExecutor()
        result = AgentOrchestrator(executor).run_if_supported("昨天各仓库实际销售出库")

        self.assertEqual(["昨天各仓库实际销售出库"], executor.calls)
        self.assertEqual("4.0", result["agent_core"]["version"])
        self.assertEqual("ALL", result["agent_core"]["scope"]["owner"])

    def test_selected_route_can_resolve_an_ambiguous_source_question(self) -> None:
        executor = FakeExecutor()
        result = AgentOrchestrator(executor).run_selected_if_supported(
            "库存周转", [{"question_id": "metric", "value": "inventory_turnover_finished_goods_realtime"}]
        )

        self.assertEqual("sales_outbound_by_warehouse", result["query_plan"]["route"])

    def test_live_evidence_is_required_before_a_number_can_leave_core(self) -> None:
        with self.assertRaises(CoreExecutionError):
            AgentOrchestrator(FakeExecutor(live=False)).run_if_supported("昨天各仓库实际销售出库")

    def test_planned_executor_classifies_once_and_executes_that_object(self) -> None:
        request = FakeRequest()
        class Executor(FakeExecutor):
            def classify(self, question):
                self.calls.append("classify")
                if len(self.calls) != 1:
                    raise AssertionError("request must not be reinterpreted")
                return request

            def execute_plan(self, plan, *, question, context_reused):
                self.calls.append("execute")
                assert plan is request and context_reused is True
                return {"query_plan": {"route": plan.route},
                        "query_execution": {"live_business_result_executed": True}}

            def run_if_supported(self, *args, **kwargs):
                raise AssertionError("legacy path must not run")

        executor = Executor()
        result = AgentOrchestrator(executor).run_if_supported("yesterday", context_reused=True)
        self.assertEqual(["classify", "execute"], executor.calls)
        self.assertEqual(request.route, result["agent_core"]["route"])

    def test_selected_plan_and_original_date_are_authoritative(self) -> None:
        request = FakeRequest(route="realtime_turnover")
        anchor = datetime.fromisoformat("2026-09-15T23:59:00+08:00")
        choices = [{"question_id": "metric", "value": "realtime"}]
        class Executor(FakeExecutor):
            def classify(self, question):
                raise AssertionError("confirmed question must not be reclassified")

            def plan_selected(self, question, selected, *, reference_time):
                assert question == "turnover" and selected is choices and reference_time is anchor
                return request

            def execute_plan(self, plan, *, question, context_reused):
                assert plan is request and context_reused
                return {"query_plan": {"route": plan.route},
                        "query_execution": {"live_business_result_executed": True}}

            def run_selected_if_supported(self, *args, **kwargs):
                raise AssertionError("legacy path must not run")

        result = AgentOrchestrator(Executor()).run_selected_if_supported(
            "turnover", choices, context_reused=True, reference_time=anchor)
        self.assertEqual("realtime_turnover", result["agent_core"]["route"])

    def test_unsupported_selected_plan_does_not_execute(self) -> None:
        class Executor(FakeExecutor):
            def plan_selected(self, *args, **kwargs):
                return None
            def execute_plan(self, *args, **kwargs):
                raise AssertionError("no executable plan")
        self.assertIsNone(AgentOrchestrator(Executor()).run_selected_if_supported("unknown", []))

    def test_plan_failure_does_not_retry_through_legacy_path(self) -> None:
        class Executor(FakeExecutor):
            def execute_plan(self, *args, **kwargs):
                raise RuntimeError("execution failed")
            def run_if_supported(self, *args, **kwargs):
                raise AssertionError("must not execute twice")
        with self.assertRaisesRegex(RuntimeError, "execution failed"):
            AgentOrchestrator(Executor()).run_if_supported("yesterday")
