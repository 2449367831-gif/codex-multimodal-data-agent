from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
from unittest.mock import patch

from app import turnover_report

from app.turnover_report import (
    NO_MOVEMENT_LABEL,
    TurnoverReportError,
    TurnoverStatus,
    TurnoverRow,
    build_turnover_report,
    build_turnover_sql,
    render_turnover_image,
    render_turnover_report,
)


class TurnoverCalculationTestCase(unittest.TestCase):
    def test_decimal_seven_day_average_and_turnover(self) -> None:
        row = TurnoverRow.from_mapping(
            {
                "wms_code": "C-01",
                "warehouse_name": "中心仓",
                "wms_type": 10,
                "belonging_center": "中心仓",
                "available_ea": "5314628",
                # 242083 per day x 7; the input contract is a seven-day total.
                "outbound_7d_ea": "1694581",
            }
        )
        self.assertEqual(Decimal("242083"), row.avg_daily_outbound_ea)
        self.assertEqual(Decimal("5314628") / Decimal("242083"), row.turnover_days)
        self.assertEqual(TurnoverStatus.OK, row.status)

    def test_zero_movement_is_explicit_and_never_divides(self) -> None:
        row = TurnoverRow("W-01", "无动销仓", "100", "0", wms_type=11, belonging_center="中心")
        self.assertIsNone(row.turnover_days)
        self.assertEqual(TurnoverStatus.NO_MOVEMENT, row.status)
        self.assertEqual(NO_MOVEMENT_LABEL, "\u65e0\u52a8\u9500")
        self.assertEqual("no_movement", row.as_dict()["status"])

    def test_negative_values_and_average_input_are_rejected(self) -> None:
        with self.assertRaises(TurnoverReportError):
            TurnoverRow("W-01", "仓", "-1", "2")
        with self.assertRaises(TurnoverReportError):
            TurnoverRow.from_mapping(
                {
                    "wms_code": "W-01",
                    "warehouse_name": "仓",
                    "available_ea": 1,
                    "avg_daily_outbound_ea": 2,
                }
            )

    def test_hierarchy_node_takes_precedence_over_legacy_wms_type(self) -> None:
        center = TurnoverRow.from_mapping(
            {
                "wms_code": "C-01",
                "warehouse_name": "中心",
                "wms_node": 10,
                "wms_type": 11,
                "available_ea": 10,
                "outbound_7d_ea": 7,
            }
        )
        detail = TurnoverRow.from_mapping(
            {
                "wms_code": "S-01",
                "warehouse_name": "分仓",
                "wms_node": 12,
                "wms_type": 10,
                "belonging_center": "中心",
                "available_ea": 10,
                "outbound_7d_ea": 7,
            }
        )
        report = build_turnover_report(
            [center, detail], date(2026, 8, 22), date(2026, 8, 29)
        )
        self.assertEqual(1, len(report.center_rows))
        self.assertEqual(1, len(report.detail_rows))

    def test_node10_with_missing_or_wrong_parent_is_still_a_center(self) -> None:
        """A bad source parent must be audited, never used to drop node-10 facts."""

        report = build_turnover_report(
            [
                {
                    "wms_code": "CENTER-MISSING-PARENT",
                    "warehouse_name": "独立中心",
                    "wms_node": 10,
                    "wms_type": 10,
                    "belonging_center": None,
                    "available_ea": 12,
                    "outbound_7d_ea": 7,
                    "source_max_stock_change_time": "2026-08-29 00:00:00",
                    "source_max_ship_at": "2026-08-28 23:59:59",
                },
                {
                    "wms_code": "CENTER-WRONG-PARENT",
                    "warehouse_name": "异常父指针中心",
                    "wms_node": 10,
                    "wms_type": 10,
                    "belonging_center": "FACTORY-17",
                    "available_ea": 8,
                    "outbound_7d_ea": 0,
                    "source_max_stock_change_time": "2026-08-29 00:00:00",
                    "source_max_ship_at": "2026-08-28 23:59:59",
                },
            ],
            date(2026, 8, 22),
            date(2026, 8, 29),
        )
        self.assertEqual(
            {"CENTER-MISSING-PARENT", "CENTER-WRONG-PARENT"},
            {row.wms_code for row in report.center_rows},
        )
        self.assertEqual((), report.detail_rows)
        self.assertTrue(report.reconciliation.passed)

    def test_unmapped_nonzero_detail_blocks_report(self) -> None:
        report = build_turnover_report(
            [
                {
                    "wms_code": "S-UNMAPPED",
                    "warehouse_name": "未知分仓",
                    "wms_node": 12,
                    "available_ea": 1,
                    "outbound_7d_ea": 0,
                    "source_max_stock_change_time": "2026-08-29 00:00:00",
                }
            ],
            date(2026, 8, 22),
            date(2026, 8, 29),
        )
        self.assertFalse(report.reconciliation.passed)
        self.assertIn("unmapped_nonzero_rows", {c.name for c in report.reconciliation.failures})

    def test_unknown_layer_type_blocks_report(self) -> None:
        report = build_turnover_report(
            [
                {
                    "wms_code": "W-UNKNOWN",
                    "warehouse_name": "未知类型仓",
                    "wms_node": 99,
                    "wms_type": 99,
                    "available_ea": 1,
                    "outbound_7d_ea": 1,
                }
            ],
            date(2026, 8, 22),
            date(2026, 8, 29),
        )
        self.assertFalse(report.reconciliation.passed)
        self.assertIn("known_warehouse_layer_type", {c.name for c in report.reconciliation.failures})


class TurnoverReconciliationTestCase(unittest.TestCase):
    def _rows(self, *, mismatch: bool = False) -> list[dict[str, object]]:
        return [
            {
                "wms_code": "C-01",
                "warehouse_name": "中心仓",
                "wms_type": 10,
                "belonging_center": "中心仓",
                "available_ea": "300",
                "outbound_7d_ea": "70",
                "source_max_stock_change_time": "2026-08-29 00:00:00",
                "source_max_ship_at": "2026-08-28 23:59:59",
            },
            {
                "wms_code": "S-01",
                "warehouse_name": "分仓一",
                "wms_type": 11,
                "belonging_center": "中心仓",
                "available_ea": "100",
                "outbound_7d_ea": "35",
                "source_max_stock_change_time": "2026-08-29 00:00:00",
                "source_max_ship_at": "2026-08-28 23:59:59",
            },
            {
                "wms_code": "S-02",
                "warehouse_name": "分仓二",
                "wms_type": 11,
                "belonging_center": "中心仓",
                "available_ea": "200" if not mismatch else "199",
                "outbound_7d_ea": "35",
                "source_max_stock_change_time": "2026-08-29 00:00:00",
                "source_max_ship_at": "2026-08-28 23:59:59",
            },
        ]

    def test_center_and_detail_sums_reconcile(self) -> None:
        report = build_turnover_report(
            self._rows(), date(2026, 8, 22), date(2026, 8, 29), profile_name="测试口径"
        )
        self.assertEqual(7, report.window_days)
        self.assertTrue(report.reconciliation.passed)
        self.assertEqual(Decimal("300"), report.center_total.available_ea)
        self.assertEqual(Decimal("10"), report.center_total.avg_daily_outbound_ea)
        self.assertEqual(Decimal("300") / Decimal("10"), report.center_total.turnover_days)

    def test_rollup_scope_adds_center_and_governed_detail_rows_once(self) -> None:
        report = build_turnover_report(
            self._rows(),
            date(2026, 8, 22),
            date(2026, 8, 29),
            center_summary_mode="rollup_scope",
        )
        self.assertEqual(Decimal("600"), report.center_total.available_ea)
        self.assertEqual(Decimal("140"), report.center_total.outbound_7d_ea)
        self.assertEqual(Decimal("30"), report.center_total.turnover_days)
        self.assertTrue(report.reconciliation.passed)
        self.assertEqual("rollup_scope", report.center_summary_mode)

    def test_center_and_detail_mismatch_is_not_cross_layer_blocking(self) -> None:
        report = build_turnover_report(
            self._rows(mismatch=True), date(2026, 8, 22), date(2026, 8, 29)
        )
        self.assertTrue(report.reconciliation.passed)
        cross_layer = [
            check
            for check in report.reconciliation.checks
            if check.name == "cross_layer:中心仓"
        ]
        self.assertEqual(1, len(cross_layer))
        self.assertEqual("not_applicable", cross_layer[0].severity)

    def test_variable_complete_window_is_supported(self) -> None:
        report = build_turnover_report(
            self._rows(), date(2026, 8, 15), date(2026, 8, 29), window_days=14
        )
        self.assertEqual(14, report.window_days)
        self.assertEqual(Decimal("5"), report.center_total.average_daily_outbound(14))
        self.assertEqual(Decimal("60"), report.center_total.turnover_days_for(14))
        self.assertTrue(report.reconciliation.passed)

    def test_image_formula_uses_requested_days_not_legacy_seven_day_default(self) -> None:
        report = build_turnover_report(self._rows(), date(2026, 8, 15), date(2026, 8, 29), window_days=14)
        with patch("app.turnover_report._draw_left", wraps=turnover_report._draw_left) as draw:
            render_turnover_image(report)
        labels = [str(call.args[2]) for call in draw.call_args_list]
        self.assertTrue(any("14日有效出库量 ÷ 14" in label for label in labels))
        self.assertFalse(any("7日有效出库量 ÷ 7" in label for label in labels))

    def test_window_changes_flow_through_dates_calculation_and_reconciliation(self) -> None:
        for days in (1, 7, 14, 21, 30, 60, 90):
            for end in (date(2026, 9, 11), date(2026, 1, 5), date(2024, 3, 1)):
                with self.subTest(days=days, end=end):
                    start = end - timedelta(days=days)
                    rows = self._rows()
                    for row in rows:
                        row["source_max_stock_change_time"] = f"{end.isoformat()} 00:00:00"
                        row["source_max_ship_at"] = f"{(end - timedelta(days=1)).isoformat()} 23:59:59"
                    report = build_turnover_report(rows, start, end, window_days=days)
                    self.assertEqual(days, report.window_days)
                    self.assertEqual(Decimal(70) / days, report.center_total.average_daily_outbound(days))
                    self.assertEqual(Decimal(300) / (Decimal(70) / days), report.center_total.turnover_days_for(days))
                    self.assertTrue(report.reconciliation.passed)
                    sql = build_turnover_sql(start.isoformat(), end.isoformat(), window_days=days)
                    self.assertIn(f"'{start.isoformat()} 00:00:00'", sql)
                    self.assertIn(f"'{end.isoformat()} 00:00:00'", sql)

    def test_same_named_centers_are_separated_by_center_code(self) -> None:
        """A display-name collision must not combine independent center trees."""

        rows = [
            {
                "wms_code": "CENTER-A",
                "warehouse_name": "同名中心",
                "wms_node": 10,
                "wms_type": 10,
                "belonging_center": "同名中心",
                "belonging_center_code": "CENTER-A",
                "available_ea": 100,
                "outbound_7d_ea": 70,
                "source_max_stock_change_time": "2026-08-28 12:00:00",
                "source_max_ship_at": "2026-08-28 12:00:00",
            },
            {
                "wms_code": "CENTER-B",
                "warehouse_name": "同名中心",
                "wms_node": 10,
                "wms_type": 10,
                "belonging_center": "同名中心",
                "belonging_center_code": "CENTER-B",
                "available_ea": 200,
                "outbound_7d_ea": 140,
                "source_max_stock_change_time": "2026-08-28 12:00:00",
                "source_max_ship_at": "2026-08-28 12:00:00",
            },
            {
                "wms_code": "DETAIL-A",
                "warehouse_name": "同名中心-分仓A",
                "wms_node": 12,
                "wms_type": 11,
                "belonging_center": "同名中心",
                "belonging_center_code": "CENTER-A",
                "available_ea": 10,
                "outbound_7d_ea": 7,
                "source_max_stock_change_time": "2026-08-28 12:00:00",
                "source_max_ship_at": "2026-08-28 12:00:00",
            },
            {
                "wms_code": "DETAIL-B",
                "warehouse_name": "同名中心-分仓B",
                "wms_node": 12,
                "wms_type": 11,
                "belonging_center": "同名中心",
                "belonging_center_code": "CENTER-B",
                "available_ea": 20,
                "outbound_7d_ea": 14,
                "source_max_stock_change_time": "2026-08-28 12:00:00",
                "source_max_ship_at": "2026-08-28 12:00:00",
            },
        ]

        report = build_turnover_report(rows, date(2026, 8, 22), date(2026, 8, 29))

        self.assertEqual(2, len(report.groups))
        groups_by_code = {
            group.center.belonging_center_code: group for group in report.groups
        }
        self.assertEqual({"CENTER-A", "CENTER-B"}, set(groups_by_code))
        self.assertEqual(("DETAIL-A",), tuple(row.wms_code for row in groups_by_code["CENTER-A"].details))
        self.assertEqual(("DETAIL-B",), tuple(row.wms_code for row in groups_by_code["CENTER-B"].details))
        self.assertEqual(Decimal("100"), groups_by_code["CENTER-A"].center.available_ea)
        self.assertEqual(Decimal("200"), groups_by_code["CENTER-B"].center.available_ea)

    def test_missing_outbound_freshness_blocks_nonzero_outbound_rows(self) -> None:
        rows = self._rows()
        for row in rows:
            row["source_max_ship_at"] = None
        report = build_turnover_report(rows, date(2026, 8, 22), date(2026, 8, 29))

        self.assertFalse(report.reconciliation.passed)
        outbound_checks = [
            check
            for check in report.reconciliation.checks
            if check.name == "outbound_freshness"
        ]
        self.assertEqual(1, len(outbound_checks))
        self.assertFalse(outbound_checks[0].passed)
        self.assertEqual("blocking", outbound_checks[0].severity)
        self.assertIn("missing=", outbound_checks[0].message)


class TurnoverSqlAndRendererTestCase(unittest.TestCase):
    def test_sql_is_single_read_only_seven_day_template(self) -> None:
        sql = build_turnover_sql("2026-08-22", "2026-08-29", cargo_owner_id="HJXH")
        upper = sql.upper()
        self.assertIn("WITH WAREHOUSE_SCOPE_RAW AS", upper)
        self.assertIn("STOCK_BY_SKU AS", upper)
        self.assertIn("'2026-08-22 00:00:00'", sql)
        self.assertIn("'2026-08-29 00:00:00'", sql)
        record_sql = build_turnover_sql(
            "2026-08-22",
            "2026-08-29",
            cargo_owner_id="HJXH",
            inventory_basis="record_deductions",
            inventory_status="ALL",
        )
        self.assertIn("INTERVAL 60 DAY", record_sql.upper())
        self.assertIn("WMS_NODE AS STRING)) IN ('10', '12', '13')", upper)
        self.assertIn("WMS_TYPE AS STRING)) IN ('10', '11')", upper)
        self.assertIn("BELONGING_CENTER", upper)
        self.assertIn("CENTER_PARENT_ANOMALY_COUNT", upper)
        self.assertIn("WHEN D.WMS_NODE = '10'", upper)
        self.assertNotIn(":START_INCLUSIVE", upper)
        self.assertNotIn(":END_EXCLUSIVE", upper)
        self.assertNotIn("DROP ", upper)
        with self.assertRaises(TurnoverReportError):
            build_turnover_sql("2026-08-22", "2026-08-28")

    def test_sql_accepts_explicit_fourteen_day_window(self) -> None:
        sql = build_turnover_sql("2026-08-15", "2026-08-29", window_days=14)
        self.assertIn("'2026-08-15 00:00:00'", sql)
        self.assertIn("'2026-08-29 00:00:00'", sql)

    def test_sql_groups_inventory_by_trimmed_sku_key(self) -> None:
        sql = build_turnover_sql("2026-08-22", "2026-08-29", cargo_owner_id="HJXH")
        stock_match = re.search(
            r"stock_raw_by_sku\s+AS\s*\((?P<body>.*?)\),\s*stock_by_sku\s+AS",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(stock_match)
        stock_body = stock_match.group("body")
        self.assertRegex(stock_body, r"TRIM\(s\.sku_code\)\s+AS\s+sku_code")
        self.assertRegex(
            stock_body,
            r"JOIN\s+sku_scope\s+AS\s+ss\s+ON\s+ss\.sku_code\s*=\s*TRIM\(s\.sku_code\)",
        )
        self.assertRegex(
            stock_body,
            r"GROUP\s+BY\s+TRIM\(s\.cargo_owner_id\)\s*,\s*TRIM\(s\.wms_code\)\s*,\s*TRIM\(s\.sku_code\)",
        )

    def test_renderer_writes_dynamic_nonempty_image(self) -> None:
        rows = [
            {
                "wms_code": "C-01",
                "warehouse_name": "中心仓（一个较长的中文名称用于测试换行）",
                "wms_type": 10,
                "belonging_center": "中心仓（一个较长的中文名称用于测试换行）",
                "available_ea": 5314628,
                "outbound_7d_ea": 1694581,
            },
            {
                "wms_code": "S-01",
                "warehouse_name": "分仓一",
                "wms_type": 11,
                "belonging_center": "中心仓（一个较长的中文名称用于测试换行）",
                "available_ea": 5314628,
                "outbound_7d_ea": 1694581,
            },
        ]
        report = build_turnover_report(rows, date(2026, 8, 22), date(2026, 8, 29))
        with TemporaryDirectory(prefix="turnover-render-test-") as directory:
            output = Path(directory) / "turnover.png"
            returned = render_turnover_report(report, output)
            self.assertEqual(output, returned)
            self.assertTrue(output.exists())
            image = render_turnover_image(report)
            self.assertGreater(image.width, 900)
            self.assertGreater(image.height, 500)
            self.assertGreater(sum(image.convert("RGB").getbbox() or (0, 0, 0, 0)), 0)


if __name__ == "__main__":
    unittest.main()
