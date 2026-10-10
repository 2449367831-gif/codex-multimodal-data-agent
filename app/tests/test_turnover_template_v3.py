from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from app.operating_turnover import build_operating_center_turnover_sql
from app.turnover_report import build_turnover_report, render_turnover_image, TurnoverReportError
from app.turnover_service import TurnoverReportRuntime, TurnoverRuntimeError
from app.turnover_template import load_turnover_template

ROOT = Path(__file__).resolve().parents[2]


def fixture_rows():
    template = load_turnover_template(ROOT)
    result = []
    for w in template.warehouses:
        result.append(dict(
            cargo_owner_id="HJXH", wms_code=w.code, warehouse_name=w.name,
            wms_type="10", wms_node="10" if w.is_center else "13",
            belonging_center_code=w.center_code, belonging_center=w.center_name,
            center_order=w.center_order, available_ea=2800,
            outbound_7d_ea=2800, center_outbound_ea=700,
            inventory_row_count=1, outbound_row_count=2, center_outbound_row_count=1,
            negative_stock_count=0, negative_outbound_count=0, duplicate_inventory_key_count=0,
            source_max_ship_at="2026-09-19 15:00:00",
            source_max_inventory_update_time="2026-09-20 00:00:00",
            source_table_max_inventory_update_time="2026-09-20 00:00:00",
            source_table_max_outbound_ingestion_update_time="2026-09-20 00:00:00",
            source_table_max_wms_ingestion_update_time="2026-09-20 00:00:00",
            source_table_max_sku_ingestion_update_time="2026-09-20 00:00:00",
            warehouse_scope_row_count=51, warehouse_center_row_count=9, warehouse_detail_row_count=42,
            warehouse_mapping_conflict_count=0, invalid_parent_mapping_count=0, unresolved_mapping_count=0,
            center_parent_anomaly_count=23, center_name_mismatch_count=0,
            source_table_inventory_row_count=51, source_table_outbound_header_count=102,
            ambiguous_outbound_count=0, control_inventory_total_ea=142800,
            control_outbound_total_ea=142800, control_center_outbound_total_ea=35700,
            control_inventory_row_count=51, control_outbound_row_count=102, control_center_outbound_row_count=51,
            control_negative_stock_count=0, control_negative_outbound_count=0,
            inventory_missing_warehouse_count=0, inventory_name_conflict_count=0,
            control_duplicate_inventory_key_count=0, expected_center_count=9,
        ))
    return result


def fixture_reports():
    rows = fixture_rows()
    common = dict(center_summary_mode="rollup_scope", inventory_freshness_mode="table_update_time")
    details = build_turnover_report(rows, "2026-09-06", "2026-09-20", window_days=14, **common)
    centers = build_turnover_report([dict(r, outbound_7d_ea=r["center_outbound_ea"]) for r in rows],
                                    "2026-09-13", "2026-09-20", window_days=7, **common)
    return centers, details


class MemoryArtifacts:
    def register_bytes(self, content, **kwargs):
        assert content.startswith(b"\x89PNG")
        return {"period": kwargs["period"], "url": "/v1/artifacts/test"}


class TemplateV3Tests(unittest.TestCase):
    def test_exact_scope_and_independent_center(self):
        t = load_turnover_template(ROOT)
        self.assertEqual(51, len(t.warehouses))
        parents = {w.code: w.center_code for w in t.warehouses}
        self.assertEqual("WMS000296", parents["WMS000296"])
        self.assertEqual("WMS000296", parents["WMS000153"])
        self.assertEqual("9813421", parents["WMS000206"])
        self.assertEqual("WMS000245", parents["P371CSA"])
        self.assertEqual("WMS000232", parents["P451CSA"])
        self.assertNotIn("P395CSB", parents)

    def test_single_statement_distinct_half_open_windows(self):
        import sqlglot
        sql = build_operating_center_turnover_sql("2026-09-06", "2026-09-20", window_days=14,
                                                warehouse_template=load_turnover_template(ROOT))
        self.assertEqual(1, len(sqlglot.parse(sql, read="doris")))
        self.assertIn("h.ship_at >= '2026-09-06 00:00:00'", sql)
        self.assertIn("h.ship_at < '2026-09-20 00:00:00'", sql)
        self.assertIn("e.business_at >= '2026-09-13 00:00:00'", sql)
        self.assertIn("h.created >= '2026-09-07 00:00:00'", sql)
        self.assertIn("h.created < '2026-09-21 00:00:00'", sql)
        self.assertIn("AS center_outbound_ea", sql)
        outbound = next(c for c in sqlglot.parse_one(sql, read="doris").find_all(sqlglot.exp.CTE) if c.alias == "outbound_by_warehouse")
        self.assertNotIn("allocate_qty", outbound.sql())
        self.assertIn("sales_current", outbound.sql())
        self.assertIn("i.ship_qty", sql)
        self.assertIn("d.request_qty", sql)
        self.assertIn("9 AS expected_center_count", sql)

    def test_separate_denominators_and_rollup(self):
        c, d = fixture_reports()
        self.assertTrue(c.reconciliation.passed)
        self.assertTrue(d.reconciliation.passed)
        self.assertEqual(Decimal(28), c.center_total.turnover_days_for(7))
        self.assertEqual(Decimal(14), d.detail_rows[0].turnover_days_for(14))
        self.assertEqual(Decimal(142800), c.center_total.available_ea)
        self.assertEqual(Decimal(5100), c.center_total.average_daily_outbound(7))

    def test_render_and_reject_mixed_inventory(self):
        c, d = fixture_reports()
        t = load_turnover_template(ROOT)
        for width in (900, 2200):
            img = render_turnover_image(d, center_report=c, template=t, width=width, reference_style=True)
            self.assertEqual(width, img.width)
            self.assertNotEqual(img.getpixel((2, 32)), (255, 255, 255))
        bad = build_turnover_report([dict(r, available_ea=2801) for r in fixture_rows()], "2026-09-13", "2026-09-20", window_days=7, center_summary_mode="rollup_scope")
        with self.assertRaises(TurnoverReportError):
            render_turnover_image(d, center_report=bad, template=t, reference_style=True)

    def runtime(self, rows):
        response = {"structuredContent": {"status": "succeeded", "execution": {
            "columns": list(rows[0]), "rows": rows, "row_count": len(rows),
            "truncated": False, "is_mock": False, "executor_name": "doris"}}}
        client = SimpleNamespace(query=lambda **kwargs: response)
        kb = SimpleNamespace(project_root=ROOT, metric=lambda _: {"filters": {"template_profile": "profiles/hjxh_turnover_template_v3.yaml"}})
        return TurnoverReportRuntime(artifact_store=MemoryArtifacts(), project_root=ROOT,
            query_client=client, knowledge=kb, clock=lambda *_: datetime(2026, 9, 20, 8))

    def test_runtime_table_uses_center_seven_days(self):
        runtime = self.runtime(fixture_rows())
        with patch.object(runtime, "_assert_contract_released"):
            result = runtime.run(principal_hash="fixture", conversation_id="fixture", request_id="fixture", window_days=14)
        self.assertEqual("经营中心7日周转", result["business_table"]["title"])
        self.assertEqual("center_warehouse_turnover_7_14", result["business_table"]["table_id"])
        self.assertEqual("28.00", result["business_table"]["rows"][0]["turnover_text"])
        self.assertIn("分仓14天", result["period"])
        self.assertEqual(9, len(result["business_table"]["rows"]))
        self.assertEqual(2, len(result["business_tables"]))
        self.assertEqual("center_warehouse_turnover_14_detail", result["business_tables"][1]["table_id"])
        self.assertGreater(len(result["business_tables"][1]["rows"]), 0)

    def test_runtime_blocks_missing_or_mismatched_seven_day_evidence(self):
        for bad in ("center_outbound_ea", "control_center_outbound_total_ea"):
            rows = fixture_rows()
            rows[0][bad] = None if bad == "center_outbound_ea" else 999
            runtime = self.runtime(rows)
            with patch.object(runtime, "_assert_contract_released"), self.assertRaises(TurnoverRuntimeError):
                runtime.run(principal_hash="fixture", conversation_id="fixture", request_id="fixture", window_days=14)

    def test_standard_template_no_longer_asks_w37_question(self):
        from app.chat_api import _is_business_window_ambiguous, is_standard_center_report
        question = "生成中心仓和分仓近14天周转数据图片"
        self.assertTrue(is_standard_center_report(question))
        self.assertFalse(_is_business_window_ambiguous(question))
        self.assertTrue(_is_business_window_ambiguous("复现W37近14天业务表"))


if __name__ == "__main__":
    unittest.main()
