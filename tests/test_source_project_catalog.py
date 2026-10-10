from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "knowledge" / "source-project" / "logic-catalog.yaml"


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject silent key overwrites when the catalog is edited by hand."""


def _construct_mapping_without_duplicates(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


class SourceProjectCatalogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = yaml.load(CATALOG_PATH.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)

    def test_catalog_is_json_serializable_and_has_expected_shape(self) -> None:
        # The runtime passes this document through JSON to the model.
        json.dumps(self.catalog, ensure_ascii=False)
        self.assertEqual("imported_candidate", self.catalog["status"])
        self.assertEqual("2026-08-25", self.catalog["source_project"]["snapshot_date"])
        families = self.catalog["logic_families"]
        self.assertEqual(16, len(families))
        self.assertEqual(
            {
                "inventory.realtime_hjxh_operating",
                "inventory.recent_production_60d_3d_priority",
                "pickup.offline_availability",
                "sales.channel_outbound_community_replacement",
                "sales.valid_outbound_project",
                "dashboard.lingshi_henmang",
                "sku.active_90d",
                "report.channel_bag_weekly",
                "alert.online_inventory_region",
                "report.tiger_sausage_inventory",
                "supply_chain.cockpit",
                "warehouse.tonnage",
                "inventory.stale_365d",
                "dms.shortage_registration",
                "report.dezhou_inventory_sales_purchase",
                "report.deliverable_sql_package_20240824",
            },
            {family["id"] for family in families},
        )

    def test_every_catalog_source_file_exists_and_is_reachable(self) -> None:
        references = []
        for family in self.catalog["logic_families"]:
            self.assertTrue(family.get("source_files"), family["id"])
            for source_file in family["source_files"]:
                references.append((family["id"], source_file))
                path = PROJECT_ROOT / source_file
                self.assertTrue(path.is_file(), f"{family['id']}: {source_file}")

        self.assertEqual(35, len(references))
        self.assertEqual(31, len({source_file for _, source_file in references}))
        for source_file in self.catalog["source_project"]["primary_documents"]:
            self.assertTrue((PROJECT_ROOT / source_file).is_file(), source_file)
        imported_scripts = self.catalog["source_project"]["imported_script_files"]
        self.assertEqual(16, len(imported_scripts))
        self.assertEqual(16, len(set(imported_scripts)))
        for source_file in imported_scripts:
            self.assertTrue((PROJECT_ROOT / source_file).is_file(), source_file)

    def test_auxiliary_tables_used_by_imported_sql_are_explicitly_tracked(self) -> None:
        inventory = self.catalog["source_table_inventory"]
        self.assertIn("ywzt_ods.sys_enum", inventory["auxiliary_lookup_tables"])
        deliverables = next(
            family
            for family in self.catalog["logic_families"]
            if family["id"] == "report.deliverable_sql_package_20240824"
        )
        self.assertIn("ywzt_ods.sys_enum", deliverables["source_tables"])
        dezhou = next(
            family
            for family in self.catalog["logic_families"]
            if family["id"] == "report.dezhou_inventory_sales_purchase"
        )
        self.assertIn("ywzt_ods.scm_warehous_manage_backups", dezhou["source_tables"])

    def test_every_declared_family_table_has_a_contract_or_explicit_boundary(self) -> None:
        inventory = self.catalog["source_table_inventory"]
        classified = {
            str(table).lower()
            for key in ("missing_doris_contracts", "separate_sources", "auxiliary_lookup_tables")
            for table in inventory.get(key, [])
        }
        for path in (PROJECT_ROOT / "sources" / "tables").glob("*.yaml"):
            source = yaml.safe_load(path.read_text(encoding="utf-8"))
            classified.add(str(source["id"]).lower())

        used = {
            str(table).lower()
            for family in self.catalog["logic_families"]
            for table in family.get("source_tables", [])
        }
        self.assertEqual(set(), used - classified)

    def test_imported_sources_are_reference_only_and_do_not_embed_secrets(self) -> None:
        self.assertFalse(self.catalog["governance"]["imported_logic_is_executable"])
        source_root = PROJECT_ROOT / "knowledge" / "source-project"
        secret_patterns = (
            re.compile(r"sk-[A-Za-z0-9]{20,}"),
            re.compile(r"(?i)(?:password|secret)\s*[:=]\s*['\"][^'\"]+['\"]"),
        )
        for path in source_root.rglob("*"):
            if not path.is_file() or path == CATALOG_PATH:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in secret_patterns:
                self.assertIsNone(pattern.search(text), f"possible secret in {path}")

    def test_catalog_counts_match_imported_files(self) -> None:
        scope = self.catalog["source_project"]["imported_scope"]
        sql_files = list((PROJECT_ROOT / "knowledge" / "source-project" / "sql").rglob("*.sql"))
        self.assertEqual(scope["imported_core_sql_files"] + scope["imported_deliverable_sql_files"], len(sql_files))
        script_files = list((PROJECT_ROOT / "knowledge" / "source-project").rglob("*.py"))
        self.assertEqual(scope["doris_query_scripts"], len(script_files))
        # Provenance is useful during refreshes; every imported file has stable bytes.
        for path in sql_files:
            self.assertEqual(64, len(hashlib.sha256(path.read_bytes()).hexdigest()))


if __name__ == "__main__":
    unittest.main()
