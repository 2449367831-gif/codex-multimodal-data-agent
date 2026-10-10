from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

try:
    import langgraph  # noqa: F401
except ImportError:  # pragma: no cover
    langgraph = None

from app.agent import AgentWorkflow
from app.codex_agent import fallback_interaction_for_question
from app.service import QueryService
from app.settings import RuntimeSettings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class T39DiagnosticRegressionTestCase(unittest.TestCase):
    def test_business_and_doris_mismatch_uses_ordered_diagnosis_without_claiming_data(self) -> None:
        response = fallback_interaction_for_question(
            "业务库查到某出库单，Doris 数仓查不到。请设计排查顺序，区分数据未同步、逻辑删除、状态过滤、连接键不一致、时间字段时区和事实表延迟。"
        )
        self.assertEqual("completed", response["outcome"])
        self.assertIn("复合业务键", response["answer"])
        self.assertIn("事实表延迟", response["answer"])
        self.assertNotIn("SQL", response["answer"])


@unittest.skipIf(langgraph is None, "LangGraph is not installed")
class AgentWorkflowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="query-agent-graph-tests-")
        self.service = QueryService(
            RuntimeSettings(
                project_root=PROJECT_ROOT,
                executor_mode="mock",
                audit_db_path=Path(self.temp_dir.name) / "audit.sqlite3",
            )
        )
        self.agent = AgentWorkflow(self.service)
        self.now = datetime(2026, 8, 28, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_yesterday_actual_outbound_builds_plan(self) -> None:
        response = self.agent.invoke(
            question="查询昨天的实际销售出库量",
            include_sql=True,
            now=self.now,
        )
        self.assertEqual("planned", response["action"])
        self.assertEqual("sales.outbound_actual", response["plan"]["metric_id"])
        self.assertEqual("2026-08-27T00:00:00", response["plan"]["parameters"]["start_inclusive"])
        self.assertEqual("2026-08-28T00:00:00", response["plan"]["parameters"]["end_exclusive"])

    def test_explicit_sales_date_range_is_not_truncated(self) -> None:
        response = self.agent.invoke(
            question="查询2026-08-01到2026-08-20的实际销售出库量",
            now=self.now,
        )
        self.assertEqual("planned", response["action"])
        self.assertEqual("2026-08-01T00:00:00", response["plan"]["parameters"]["start_inclusive"])
        self.assertEqual("2026-08-21T00:00:00", response["plan"]["parameters"]["end_exclusive"])

    def test_missing_snapshot_date_clarifies(self) -> None:
        response = self.agent.invoke(question="查询历史库存快照", now=self.now)
        self.assertEqual("clarify", response["action"])
        self.assertIn("snapshot_date", response["missing"])

    def test_snapshot_with_two_dates_clarifies(self) -> None:
        response = self.agent.invoke(
            question="查询2026年8月20日和2026年8月21日的历史库存快照",
            now=self.now,
        )
        self.assertEqual("clarify", response["action"])
        self.assertIn("single_snapshot_date", response["missing"])

    def test_explicit_snapshot_date_builds_warehouse_sku_plan(self) -> None:
        response = self.agent.invoke(
            question="查询2026年8月20日的历史库存快照", now=self.now
        )
        self.assertEqual("planned", response["action"])
        self.assertEqual("inventory.snapshot_quantity", response["plan"]["metric_id"])
        self.assertEqual("warehouse_sku", response["plan"]["parameters"]["output_grain"])

    def test_documentation_only_turnover_is_blocked(self) -> None:
        response = self.agent.invoke(
            question="查询本月库存周转天数，销售截止今天凌晨0点",
            now=self.now,
        )
        self.assertEqual("blocked", response["action"])
        self.assertEqual("inventory.turnover_finished_goods_realtime", response["metric_id"])

    def test_order_request_variant_is_rejected(self) -> None:
        response = self.agent.invoke(
            question="查询2026年8月27日销售订单的请求发货量",
            now=self.now,
        )
        self.assertEqual("reject", response["action"])
        self.assertEqual("unsupported_metric_variant", response["reason_code"])
        self.assertIn("sales.order_requested", response["unavailable_variants"])

    def test_transit_inventory_variant_requires_one_named_choice(self) -> None:
        response = self.agent.invoke(question="查现在的在途库存", now=self.now)
        self.assertEqual("clarify", response["action"])
        self.assertEqual("transit_quantity_basis", response["clarification_key"])
        self.assertEqual(
            {
                "inventory.transit_legacy_full_requested",
                "inventory.transit_remaining_unreceived",
            },
            set(response["unavailable_variants"]),
        )

    def test_historical_turnover_uses_period_end_finished_goods_snapshot(self) -> None:
        response = self.agent.invoke(question="查询上个月的历史成品库存周转天数", now=self.now)
        self.assertEqual("blocked", response["action"])
        self.assertEqual("inventory.turnover_finished_goods_snapshot", response["metric_id"])
        self.assertEqual("2026-07-01T00:00:00", response["parameters"]["start_inclusive"].isoformat())
        self.assertEqual("2026-08-01T00:00:00", response["parameters"]["end_exclusive"].isoformat())
        self.assertEqual("2026-07-31", response["parameters"]["snapshot_date"].isoformat())

    def test_prior_sales_interval_keeps_confirmed_realtime_default(self) -> None:
        response = self.agent.invoke(question="查询上个月的库存周转天数", now=self.now)
        self.assertEqual("blocked", response["action"])
        self.assertEqual("inventory.turnover_finished_goods_realtime", response["metric_id"])
        self.assertEqual("2026-07-01T00:00:00", response["parameters"]["start_inclusive"].isoformat())
        self.assertEqual("2026-08-01T00:00:00", response["parameters"]["end_exclusive"].isoformat())
        self.assertNotIn("snapshot_date", response["parameters"])

    def test_explicit_sales_days_do_not_imply_a_historical_inventory_numerator(self) -> None:
        response = self.agent.invoke(question="查询2026年7月1日至2026年7月31日库存周转", now=self.now)
        self.assertEqual("blocked", response["action"])
        self.assertEqual("inventory.turnover_finished_goods_realtime", response["metric_id"])

    def test_explicit_current_inventory_is_allowed_for_prior_sales_interval(self) -> None:
        response = self.agent.invoke(
            question="用当前实时成品库存计算2026年7月库存周转天数",
            now=self.now,
        )
        self.assertEqual("blocked", response["action"])
        self.assertEqual("inventory.turnover_finished_goods_realtime", response["metric_id"])
        self.assertEqual("2026-07-01T00:00:00", response["parameters"]["start_inclusive"].isoformat())
        self.assertEqual("2026-08-01T00:00:00", response["parameters"]["end_exclusive"].isoformat())

    def test_generic_turnover_asks_only_missing_sales_window(self) -> None:
        response = self.agent.invoke(question="查询库存周转", now=self.now)
        self.assertEqual("clarify", response["action"])
        self.assertEqual(
            ["sales_window"],
            [question["id"] for question in response["questions"]],
        )

    def test_historical_date_is_not_dropped_from_realtime_inventory(self) -> None:
        response = self.agent.invoke(
            question="查询2026年8月20日可用库存",
            now=self.now,
        )
        self.assertEqual("reject", response["action"])
        self.assertEqual("contract_not_applicable", response["reason_code"])
        self.assertIn("historical_snapshot", response["detected_negative_signals"])
        self.assertIn("inventory.available_realtime", response["forbidden_metric_ids"])

    def test_planned_and_unshipped_are_not_routed_as_actual_outbound(self) -> None:
        planned = self.agent.invoke(
            question="查询2026年8月20日订单计划的销售出库量",
            now=self.now,
        )
        unshipped = self.agent.invoke(
            question="查询2026年8月20日未完成销售出库量",
            now=self.now,
        )
        self.assertEqual("reject", planned["action"])
        self.assertIn("sales.outbound_planned", planned["unavailable_variants"])
        self.assertEqual("reject", unshipped["action"])
        self.assertIn("sales.unshipped_demand", unshipped["unavailable_variants"])

    def test_generic_shipment_and_inventory_require_basis(self) -> None:
        shipment = self.agent.invoke(question="昨天发货量是多少", now=self.now)
        inventory = self.agent.invoke(question="现在库存有多少", now=self.now)
        self.assertEqual("clarify", shipment["action"])
        self.assertEqual("shipment_quantity_basis", shipment["clarification_key"])
        self.assertEqual("clarify", inventory["action"])
        self.assertEqual("inventory_quantity_basis", inventory["clarification_key"])

    def test_channel_dimension_is_not_channel_replacement(self) -> None:
        response = AgentWorkflow._semantic_boundary(
            "查询上周线下渠道出库数量、出库单量和箱数，按日期汇总"
        )
        self.assertIsNone(response)


if __name__ == "__main__":
    unittest.main()
