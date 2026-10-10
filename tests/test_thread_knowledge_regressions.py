from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative_path: str) -> dict:
    value = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{relative_path} must contain a mapping")
    return value


class ThreadKnowledgeRegressionTestCase(unittest.TestCase):
    def test_candidate_release_selects_latest_confirmed_sales_contract(self) -> None:
        release = load_yaml("releases/candidate-0.1.0.yaml")
        self.assertIn("sales.outbound_actual@2.0.0", release["contracts"]["metrics"])
        self.assertNotIn("sales.outbound_actual@1.0.0", release["contracts"]["metrics"])

        metric = load_yaml("metrics/sales.outbound_actual.v2.yaml")
        self.assertEqual([700, 900, 701, 901], metric["filters"]["completion_statuses"])
        self.assertIn("sales_order_non_multiplication", metric["required_validations"])
        self.assertIn("metric_validation_isolation", metric["required_validations"])

        sql = (ROOT / "sql_templates/sales.outbound_actual.v2.sql").read_text(
            encoding="utf-8"
        )
        status_clause = re.search(r"h\.status\s+IN\s*\(([^)]*)\)", sql)
        self.assertIsNotNone(status_clause)
        self.assertEqual([700, 900, 701, 901], [int(value) for value in re.findall(r"\d+", status_clause.group(1))])
        self.assertIn("EXISTS", sql)

    def test_turnover_contracts_pin_scope_equivalence_and_sales_v2(self) -> None:
        for relative_path in (
            "metrics/inventory.turnover_finished_goods_realtime.yaml",
            "metrics/inventory.turnover_finished_goods_snapshot.yaml",
            "metrics/inventory.turnover_7d_realtime.v2.yaml",
        ):
            metric = load_yaml(relative_path)
            self.assertEqual(
                "2.0.0", metric["dependency_versions"]["sales.outbound_actual"]
            )
            self.assertIn("component_scope_equivalence", metric["required_validations"])
            self.assertIn("metric_validation_isolation", metric["required_validations"])

    def test_generic_turnover_has_two_fixed_inventory_points_and_one_denominator(self) -> None:
        release = load_yaml("releases/candidate-0.1.0.yaml")
        selected = set(release["contracts"]["metrics"])
        self.assertIn("inventory.turnover_finished_goods_realtime@1.0.0", selected)
        self.assertIn("inventory.turnover_finished_goods_snapshot@1.0.0", selected)
        self.assertNotIn("inventory.turnover_mtd_realtime@2.0.0", selected)

        realtime = load_yaml("metrics/inventory.turnover_finished_goods_realtime.yaml")
        snapshot = load_yaml("metrics/inventory.turnover_finished_goods_snapshot.yaml")
        self.assertIn("available_quantity", realtime["filters"]["inventory_basis"])
        self.assertIn("in_transit_quantity", realtime["filters"]["inventory_basis"])
        self.assertIn("in_stock_quantity", snapshot["filters"]["inventory_basis"])
        for metric in (realtime, snapshot):
            self.assertIn("sku_group", metric["filters"]["fixed_sku_population"])
            self.assertIn("ship_qty", metric["filters"]["outbound_basis"])
            self.assertIn("complete_natural_days", metric["formula"])

    def test_thread_analysis_preserves_provenance_without_promoting_old_answers(self) -> None:
        rules = load_yaml("rules/thread-derived-semantics.yaml")
        provenance = rules["provenance"]
        self.assertEqual(
            "019eb0d0-51c3-73d1-b7cf-3e6acc2e0956",
            provenance["source_thread_id"],
        )
        self.assertEqual(392, provenance["corpus_summary"]["user_messages"])
        self.assertEqual(57, provenance["corpus_summary"]["explicit_business_corrections"])
        self.assertEqual(
            "source_contract_missing",
            rules["metric_families"]["in_transit"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
