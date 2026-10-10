from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from app.errors import ParameterValidationError, SqlSafetyError, TemplateNotExecutableError
from app.service import QueryService
from app.settings import RuntimeSettings
from app.sql import SqlSafetyValidator
from app.sql import TemplateCompiler


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="query-agent-tests-")
        settings = RuntimeSettings(
            project_root=PROJECT_ROOT,
            executor_mode="mock",
            audit_db_path=Path(self.temp_dir.name) / "audit.sqlite3",
            max_rows=25,
        )
        self.service = QueryService(settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assert_contract_sql_rejected(
        self,
        sql: str,
        *,
        validations: list[str],
    ) -> None:
        with TemporaryDirectory(prefix="query-agent-contract-gate-") as directory:
            root = Path(directory)
            (root / "sql_templates").mkdir()
            (root / "sql_templates" / "unsafe.sql").write_text(sql, encoding="utf-8")
            compiler = TemplateCompiler(root)
            compiler.capabilities = {
                "sales.outbound_actual@1.0.0": {
                    "executable": True,
                    "output_grains": ["total"],
                }
            }
            metric = {
                "id": "sales.outbound_actual",
                "version": "1.0.0",
                "template": "sql_templates/unsafe.sql",
                "sources": {
                    "required": [
                        "ywzt_ods.scm_sales_shipment_header",
                        "ywzt_ods.scm_sales_shipment_detail",
                        "ywzt_ods.scm_sales_order_header",
                    ]
                },
                "required_validations": validations,
            }
            with self.assertRaises(TemplateNotExecutableError):
                compiler.compile(metric, {"output_grain": "total"})

    def test_knowledge_and_chinese_route(self) -> None:
        manifest = yaml.safe_load((PROJECT_ROOT / "releases/candidate-0.1.0.yaml").read_text(encoding="utf-8"))
        expected_ids = {ref.rsplit("@", 1)[0] for ref in manifest["contracts"]["metrics"]}
        self.assertEqual(len(expected_ids), len(self.service.list_metrics()))
        self.assertEqual(expected_ids, {metric["id"] for metric in self.service.list_metrics()})
        self.assertIn("sales.sku_name_quality", {metric["id"] for metric in self.service.list_metrics()})
        self.assertIn("sales.outbound_boxes", {metric["id"] for metric in self.service.list_metrics()})
        self.assertIn("purchase.received_detail", {metric["id"] for metric in self.service.list_metrics()})
        self.assertIn("sales.finished_goods_exceptions", {metric["id"] for metric in self.service.list_metrics()})
        ambiguous = self.service.router.route("请查库存周转天数")
        self.assertIsNone(ambiguous.selected_metric_id)
        self.assertTrue(ambiguous.needs_clarification)
        route = self.service.router.route("请查当前实时成品库存周转")
        self.assertEqual("inventory.turnover_finished_goods_realtime", route.selected_metric_id)
        self.assertFalse(route.needs_clarification)

    def test_plan_uses_bound_parameters(self) -> None:
        plan = self.service.build_plan(
            metric_id="sales.outbound_actual",
            parameters={
                "start_inclusive": "2026-08-01 00:00:00",
                "end_exclusive": "2026-08-02 00:00:00",
                "output_grain": "total",
            },
        )
        self.assertIn("%(start_inclusive)s", plan.sql)
        self.assertEqual(
            (
                "ywzt_ods.scm_sales_order_header",
                "ywzt_ods.scm_sales_shipment_detail",
                "ywzt_ods.scm_sales_shipment_header",
            ),
            plan.source_tables,
        )
        validations = {item["id"]: item for item in plan.validation_results}
        self.assertTrue(validations["shipment_header_non_multiplication"]["passed"])
        self.assertTrue(validations["sales_order_non_multiplication"]["passed"])
        self.assertIn("WITH eligible_outbound AS", plan.sql)
        self.assertIn("EXISTS", plan.sql)
        self.assertIn("LIKE 'SO%%'", plan.sql)

    def test_raw_shipment_header_join_fails_contract_static_gate(self) -> None:
        with TemporaryDirectory(prefix="query-agent-header-gate-") as directory:
            root = Path(directory)
            (root / "sql_templates").mkdir()
            (root / "sql_templates" / "unsafe.sql").write_text(
                "SELECT SUM(d.ship_qty) FROM ywzt_ods.scm_sales_shipment_header h "
                "JOIN ywzt_ods.scm_sales_shipment_detail d "
                "ON d.outbound_code = h.outbound_code",
                encoding="utf-8",
            )
            compiler = TemplateCompiler(root)
            compiler.capabilities = {
                "sales.outbound_actual@1.0.0": {
                    "executable": True,
                    "output_grains": ["total"],
                }
            }
            metric = {
                "id": "sales.outbound_actual",
                "version": "1.0.0",
                "template": "sql_templates/unsafe.sql",
                "sources": {
                    "required": [
                        "ywzt_ods.scm_sales_shipment_header",
                        "ywzt_ods.scm_sales_shipment_detail",
                    ]
                },
                "required_validations": ["shipment_header_non_multiplication"],
            }
            with self.assertRaises(TemplateNotExecutableError):
                compiler.compile(metric, {"output_grain": "total"})

    def test_shipment_header_gate_rejects_multiplication_structures(self) -> None:
        cte_prefix = (
            "WITH eligible_outbound AS (SELECT h.outbound_code "
            "FROM ywzt_ods.scm_sales_shipment_header h "
        )
        outer = (
            ") SELECT SUM(d.ship_qty) FROM eligible_outbound h "
            "JOIN ywzt_ods.scm_sales_shipment_detail d "
            "ON d.outbound_code = h.outbound_code"
        )
        cases = {
            "extra_group_key": cte_prefix
            + "GROUP BY h.outbound_code, h.id"
            + outer,
            "rollup": cte_prefix + "GROUP BY ROLLUP(h.outbound_code)" + outer,
            "union_all": (
                cte_prefix
                + "GROUP BY h.outbound_code UNION ALL SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h GROUP BY h.outbound_code"
                + outer
            ),
            "or_join": cte_prefix
            + "GROUP BY h.outbound_code"
            + outer.replace(
                "ON d.outbound_code = h.outbound_code",
                "ON d.outbound_code = h.outbound_code OR 1 = 1",
            ),
            "left_join": cte_prefix
            + "GROUP BY h.outbound_code"
            + outer.replace(
                "JOIN ywzt_ods.scm_sales_shipment_detail",
                "LEFT JOIN ywzt_ods.scm_sales_shipment_detail",
            ),
            "right_join": cte_prefix
            + "GROUP BY h.outbound_code"
            + outer.replace(
                "JOIN ywzt_ods.scm_sales_shipment_detail",
                "RIGHT JOIN ywzt_ods.scm_sales_shipment_detail",
            ),
            "full_outer_join": cte_prefix
            + "GROUP BY h.outbound_code"
            + outer.replace(
                "JOIN ywzt_ods.scm_sales_shipment_detail",
                "FULL OUTER JOIN ywzt_ods.scm_sales_shipment_detail",
            ),
            "duplicate_cte_consumer": cte_prefix
            + "GROUP BY h.outbound_code"
            + outer
            + " JOIN eligible_outbound h2 ON h2.outbound_code = h.outbound_code",
            "cte_column_remap": (
                "WITH eligible_outbound(dummy, outbound_code) AS ("
                "SELECT h.outbound_code, MAX(h.ref_order_code) "
                "FROM ywzt_ods.scm_sales_shipment_header h "
                "GROUP BY h.outbound_code"
                + outer
            ),
        }
        for name, sql in cases.items():
            with self.subTest(name=name):
                self.assert_contract_sql_rejected(
                    sql,
                    validations=["shipment_header_non_multiplication"],
                )

    def test_sales_order_gate_rejects_non_exists_or_bypass_filters(self) -> None:
        safe_outer = (
            " GROUP BY h.outbound_code) "
            "SELECT SUM(d.ship_qty) FROM eligible_outbound h "
            "JOIN ywzt_ods.scm_sales_shipment_detail d "
            "ON d.outbound_code = h.outbound_code"
        )
        exists = (
            "EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header so "
            "WHERE so.sales_order_code = h.ref_order_code)"
        )
        cases = {
            "raw_join": (
                "WITH eligible_outbound AS (SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h "
                "JOIN ywzt_ods.scm_sales_order_header so "
                "ON so.sales_order_code = h.ref_order_code"
                + safe_outer
            ),
            "or_bypass": (
                "WITH eligible_outbound AS (SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h WHERE ("
                + exists
                + " OR 1 = 1)"
                + safe_outer
            ),
            "not_exists": (
                "WITH eligible_outbound AS (SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h WHERE NOT "
                + exists
                + safe_outer
            ),
            "uncorrelated_exists": (
                "WITH eligible_outbound AS (SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h WHERE EXISTS "
                "(SELECT 1 FROM ywzt_ods.scm_sales_order_header so "
                "WHERE so.sales_order_code = so.sales_order_code)"
                + safe_outer
            ),
            "inner_or_correlation": (
                "WITH eligible_outbound AS (SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h WHERE EXISTS "
                "(SELECT 1 FROM ywzt_ods.scm_sales_order_header so "
                "WHERE 1 = 1 OR so.sales_order_code = h.ref_order_code)"
                + safe_outer
            ),
            "projected_correlation": (
                "WITH eligible_outbound AS (SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h WHERE EXISTS "
                "(SELECT so.sales_order_code = h.ref_order_code "
                "FROM ywzt_ods.scm_sales_order_header so WHERE 1 = 1)"
                + safe_outer
            ),
            "negated_correlation": (
                "WITH eligible_outbound AS (SELECT h.outbound_code "
                "FROM ywzt_ods.scm_sales_shipment_header h WHERE EXISTS "
                "(SELECT 1 FROM ywzt_ods.scm_sales_order_header so "
                "WHERE NOT (so.sales_order_code = h.ref_order_code))"
                + safe_outer
            ),
        }
        for name, sql in cases.items():
            with self.subTest(name=name):
                self.assert_contract_sql_rejected(
                    sql,
                    validations=[
                        "shipment_header_non_multiplication",
                        "sales_order_non_multiplication",
                    ],
                )

    def test_frozen_compiler_rejects_unpreloaded_template(self) -> None:
        with TemporaryDirectory(prefix="query-agent-template-seal-") as directory:
            root = Path(directory)
            (root / "app").mkdir()
            (root / "sql_templates").mkdir()
            (root / "app" / "template_capabilities.yaml").write_text(
                "version: 1.0.0\nmetrics:\n"
                "  test.metric@1.0.0: {executable: true, output_grains: [total]}\n",
                encoding="utf-8",
            )
            (root / "sql_templates" / "metric.sql").write_text(
                "SELECT * FROM ywzt_ods.safe_table",
                encoding="utf-8",
            )
            compiler = TemplateCompiler(root, reload_on_change=False)
            metric = {
                "id": "test.metric",
                "version": "1.0.0",
                "template": "sql_templates/metric.sql",
                "sources": {"required": ["ywzt_ods.safe_table"]},
            }
            with self.assertRaises(TemplateNotExecutableError):
                compiler.compile(metric, {"output_grain": "total"})

    def test_mock_query_is_audited(self) -> None:
        result = self.service.query(
            metric_id="sales.outbound_actual",
            parameters={
                "start_inclusive": "2026-08-01 00:00:00",
                "end_exclusive": "2026-08-02 00:00:00",
                "output_grain": "total",
            },
        )
        self.assertEqual("mock_incomplete", result["status"])
        audit = self.service.audit.get(result["request_id"])
        self.assertIsNotNone(audit)
        self.assertTrue(audit["is_mock"])

    def test_unknown_parameter_is_blocked_and_audited(self) -> None:
        with self.assertRaises(ParameterValidationError):
            self.service.query(
                metric_id="sales.outbound_actual",
                parameters={"unexpected": "value"},
            )
        self.assertEqual("blocked", self.service.audit.list(limit=1)[0]["status"])

    def test_documentation_only_template_is_blocked(self) -> None:
        with self.assertRaises(TemplateNotExecutableError):
            self.service.build_plan(
                metric_id="inventory.turnover_finished_goods_realtime",
                parameters={
                    "start_inclusive": "2026-08-01 00:00:00",
                    "end_exclusive": "2026-08-28 00:00:00",
                    "output_grain": "total",
                },
            )

    def test_sql_safety_rejects_multiple_statements(self) -> None:
        with self.assertRaises(SqlSafetyError):
            SqlSafetyValidator().validate(
                "SELECT * FROM ywzt_ods.safe_table; DROP TABLE ywzt_ods.safe_table;",
                allowed_sources={"ywzt_ods.safe_table"},
            )

    def test_sql_safety_checks_every_comma_joined_table(self) -> None:
        with self.assertRaises(SqlSafetyError):
            SqlSafetyValidator().validate(
                "SELECT * FROM ywzt_ods.safe_table, secret.other_table",
                allowed_sources={"ywzt_ods.safe_table"},
            )

    def test_comment_markers_inside_literals_cannot_hide_a_table(self) -> None:
        with self.assertRaises(SqlSafetyError):
            SqlSafetyValidator().validate(
                "SELECT * FROM ywzt_ods.safe_table WHERE x='/*' "
                "UNION ALL SELECT * FROM secret.other_table WHERE x='*/'",
                allowed_sources={"ywzt_ods.safe_table"},
            )

    def test_sql_safety_rejects_side_effecting_read_clauses(self) -> None:
        validator = SqlSafetyValidator()
        for sql in (
            "SELECT 1 INTO @x",
            "SELECT * FROM ywzt_ods.safe_table FOR UPDATE",
            "SELECT SLEEP(100) FROM ywzt_ods.safe_table",
        ):
            with self.subTest(sql=sql), self.assertRaises(SqlSafetyError):
                validator.validate(sql, allowed_sources={"ywzt_ods.safe_table"})

    def test_doris_template_compiler_freezes_template_content(self) -> None:
        with TemporaryDirectory(prefix="query-agent-template-freeze-") as directory:
            root = Path(directory)
            (root / "sql_templates").mkdir()
            (root / "app").mkdir()
            template_path = root / "sql_templates" / "metric.sql"
            template_path.write_text(
                "SELECT * FROM ywzt_ods.safe_table WHERE value = :value",
                encoding="utf-8",
            )
            (root / "app" / "template_capabilities.yaml").write_text(
                "version: 1.0.0\nmetrics:\n"
                "  test.metric@1.0.0:\n"
                "    executable: true\n"
                "    output_grains: [total]\n",
                encoding="utf-8",
            )
            compiler = TemplateCompiler(
                root,
                reload_on_change=False,
                preload_template_refs=("sql_templates/metric.sql",),
            )
            metric = {
                "id": "test.metric",
                "version": "1.0.0",
                "template": "sql_templates/metric.sql",
                "sources": {"required": ["ywzt_ods.safe_table"]},
            }
            first = compiler.compile(metric, {"value": 1, "output_grain": "total"})
            template_path.write_text(
                "SELECT * FROM secret.changed_table WHERE value = :value",
                encoding="utf-8",
            )
            second = compiler.compile(metric, {"value": 1, "output_grain": "total"})
            self.assertEqual(first.sql, second.sql)


if __name__ == "__main__":
    unittest.main()
