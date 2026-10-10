from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from tools.metadata_sync import (
    MetadataSyncError,
    build_freshness_query,
    build_key_quality_query,
    compare_sources,
    load_snapshot,
    load_source_tables,
    scan_doris,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeCursor:
    def __init__(self) -> None:
        self.description = None
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self._rows: list[object] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.calls.append((sql, params))
        if "information_schema.COLUMNS" in sql:
            self.description = [
                ("TABLE_SCHEMA",),
                ("TABLE_NAME",),
                ("COLUMN_NAME",),
                ("ORDINAL_POSITION",),
                ("IS_NULLABLE",),
                ("DATA_TYPE",),
                ("COLUMN_TYPE",),
                ("COLUMN_COMMENT",),
            ]
            self._rows = [
                ("ywzt_ods", "demo", "id", 1, "YES", "bigint", "bigint", ""),
                ("ywzt_ods", "demo", "event_at", 2, "YES", "datetime", "datetime", ""),
            ]
        elif "information_schema.TABLES" in sql:
            self.description = [("TABLE_SCHEMA",), ("TABLE_NAME",), ("TABLE_TYPE",), ("TABLE_COMMENT",)]
            self._rows = [("ywzt_ods", "demo", "BASE TABLE", "fixture")]
        elif sql.startswith("SELECT MAX("):
            self.description = [("max_value",)]
            self._rows = [("2026-08-28 08:59:00",)]
        elif "AS distinct_key_count" in sql:
            self.description = [
                ("row_count",),
                ("distinct_key_count",),
                ("null_key_count",),
                ("duplicate_key_count",),
            ]
            self._rows = [(10, 8, 1, 1)]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[object]:
        return self._rows

    def fetchone(self) -> object:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def rollback(self) -> None:
        return None


class VirtualFreshnessCursor:
    """Small fixture for a table whose freshness comes from TABLES.UPDATE_TIME."""

    def __init__(self) -> None:
        self.description = None
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self._rows: list[object] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.calls.append((sql, params))
        if "information_schema.COLUMNS" in sql:
            fields = [
                ("owner", "varchar"),
                ("wms_desc", "varchar"),
                ("sku_name", "varchar"),
                ("sku_code", "varchar"),
                ("available_quantity", "decimal"),
                ("total_quantity", "decimal"),
                ("instock_quantity", "decimal"),
                ("in_transit_quantity", "decimal"),
                ("allocated_quantity", "decimal"),
                ("today_outbound", "decimal"),
                ("yesterday_outbound", "decimal"),
                ("seven_days_outbound", "decimal"),
                ("available_days", "decimal"),
                ("locked_quantity", "decimal"),
                ("frozen_quantity", "decimal"),
                ("business_platform_occupied", "decimal"),
                ("purchase_in_transit", "decimal"),
                ("transfer_in_transit", "decimal"),
            ]
            self.description = [
                ("TABLE_SCHEMA",), ("TABLE_NAME",), ("COLUMN_NAME",),
                ("ORDINAL_POSITION",), ("IS_NULLABLE",), ("DATA_TYPE",),
                ("COLUMN_TYPE",), ("COLUMN_COMMENT",),
            ]
            self._rows = [
                ("ywzt_ods", "sku_inventory", name, index, "YES", data_type, data_type, "")
                for index, (name, data_type) in enumerate(fields, start=1)
            ]
        elif "information_schema.TABLES" in sql:
            self.description = [
                ("TABLE_SCHEMA",), ("TABLE_NAME",), ("TABLE_TYPE",),
                ("TABLE_COMMENT",), ("UPDATE_TIME",),
            ]
            self._rows = [
                (
                    "ywzt_ods", "sku_inventory", "BASE TABLE", "fixture",
                    "2026-08-28 08:59:00",
                )
            ]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[object]:
        return self._rows

    def fetchone(self) -> object:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        return None


class VirtualFreshnessConnection:
    def __init__(self) -> None:
        self.cursor_obj = VirtualFreshnessCursor()

    def cursor(self) -> VirtualFreshnessCursor:
        return self.cursor_obj

    def rollback(self) -> None:
        return None


class MetadataSyncTests(unittest.TestCase):
    def test_clean_snapshot_has_no_drift(self) -> None:
        sources = load_source_tables(ROOT / "sources" / "tables")
        snapshot = load_snapshot(ROOT / "metadata" / "examples" / "doris_snapshot.json")
        report = compare_sources(sources, snapshot, now=datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc))
        self.assertFalse(report.has_drift)
        self.assertEqual(report.observed_table_count, len(sources))
        self.assertEqual(report.to_dict()["summary"]["candidate_count"], 0)

    def test_drift_snapshot_reports_required_kinds(self) -> None:
        sources = load_source_tables(ROOT / "sources" / "tables")
        snapshot = load_snapshot(ROOT / "metadata" / "examples" / "doris_snapshot_drift.json")
        report = compare_sources(sources, snapshot, now=datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc))
        kinds = {item.kind for item in report.candidates}
        self.assertTrue(
            {"missing_table", "missing_column", "type_changed", "undocumented_column", "freshness"}
            <= kinds
        )

    def test_missing_table_is_high_severity(self) -> None:
        sources = load_source_tables(ROOT / "sources" / "tables")
        snapshot = load_snapshot(ROOT / "metadata" / "examples" / "doris_snapshot.json")
        snapshot.tables.pop("ywzt_ods.scm_warehous_occupy")
        report = compare_sources(sources, snapshot)
        missing = [item for item in report.candidates if item.kind == "missing_table"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].severity, "high")

    def test_scan_doris_uses_only_read_selects(self) -> None:
        source = next(
            item
            for item in load_source_tables(ROOT / "sources" / "tables")
            if item.id == "ywzt_ods.master_wms"
        )
        # Add a freshness declaration to the small fake table contract.
        source = source.__class__(
            **{
                **source.__dict__,
                "id": "ywzt_ods.demo",
                "table": "demo",
                "freshness_field": "event_at",
                "fields": {"id": {"type": "integer"}, "event_at": {"type": "datetime"}},
            }
        )
        connection = FakeConnection()
        snapshot = scan_doris(connection, [source])
        self.assertIn("ywzt_ods.demo", snapshot.tables)
        self.assertEqual(snapshot.tables["ywzt_ods.demo"].freshness["event_at"].max_value, "2026-08-28 08:59:00")
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql, _ in connection.cursor_obj.calls))
        self.assertTrue(all(";" not in sql for sql, _ in connection.cursor_obj.calls))

    def test_identifier_is_rejected_before_query(self) -> None:
        with self.assertRaises(MetadataSyncError):
            build_freshness_query("ywzt_ods", "demo", "event_at; DROP TABLE x")

    def test_key_quality_query_is_bounded_and_aggregate_only(self) -> None:
        sql = build_key_quality_query(
            "ywzt_ods", "demo", "id", "event_at", 30
        )
        self.assertTrue(sql.startswith("SELECT COUNT(*)"))
        self.assertIn("INTERVAL 30 DAY", sql)
        self.assertNotIn("GROUP BY", sql.upper())
        self.assertNotIn(";", sql)
        self.assertNotIn("sample", sql.casefold())
        for invalid in (0, 3651, True, "30"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(MetadataSyncError):
                    build_key_quality_query(
                        "ywzt_ods", "demo", "id", "event_at", invalid  # type: ignore[arg-type]
                    )

    def test_key_quality_scan_is_explicit_and_uses_read_only_gate(self) -> None:
        original = next(
            item
            for item in load_source_tables(ROOT / "sources" / "tables")
            if item.id == "ywzt_ods.scm_sales_shipment_header"
        )
        source = original.__class__(
            **{
                **original.__dict__,
                "id": "ywzt_ods.demo",
                "table": "demo",
                "business_key": "id",
                "freshness_field": "event_at",
                "freshness_config": {"field": "event_at"},
                "key_quality_enabled": True,
                "key_quality_lookback_days": 30,
                "key_quality_config": {"enabled": True, "lookback_days": 30},
                "fields": {
                    "id": {"type": "integer"},
                    "event_at": {"type": "datetime"},
                },
            }
        )
        connection = FakeConnection()
        snapshot = scan_doris(connection, [source])
        quality = snapshot.tables["ywzt_ods.demo"].key_quality["id"]
        self.assertEqual(quality.row_count, 10)
        self.assertEqual(quality.distinct_key_count, 8)
        self.assertEqual(quality.null_key_count, 1)
        self.assertEqual(quality.duplicate_key_count, 1)
        self.assertEqual(quality.status, "warning")
        quality_queries = [
            sql for sql, _ in connection.cursor_obj.calls if "AS distinct_key_count" in sql
        ]
        self.assertEqual(len(quality_queries), 1)
        self.assertIn("INTERVAL 30 DAY", quality_queries[0])
        self.assertTrue(
            all(sql.lstrip().upper().startswith("SELECT") for sql, _ in connection.cursor_obj.calls)
        )
        self.assertTrue(all(";" not in sql for sql, _ in connection.cursor_obj.calls))

    def test_declared_business_keys_enable_bounded_key_quality(self) -> None:
        enabled = [
            item.id
            for item in load_source_tables(ROOT / "sources" / "tables")
            if item.key_quality_enabled
        ]
        self.assertEqual(
            enabled,
            [
                "dms_ods.t_employee",
                "dms_ods.t_sales_order",
                "dms_ods.t_sales_order_detail",
                "ywzt_ods.master_wms",
                "ywzt_ods.scm_sales_order_detail",
                "ywzt_ods.scm_sales_order_header",
                "ywzt_ods.scm_sales_shipment_header",
                "ywzt_ods.scm_warehous_detail",
                "ywzt_ods.scm_warehous_header",
            ],
        )

    def test_virtual_table_freshness_skips_physical_column_drift(self) -> None:
        source = next(
            item
            for item in load_source_tables(ROOT / "sources" / "tables")
            if item.id == "ywzt_ods.sku_inventory"
        )
        self.assertFalse(source.key_quality_enabled)
        self.assertTrue(source.fields["ingestion_update_time"].get("virtual"))

        connection = VirtualFreshnessConnection()
        snapshot = scan_doris(
            connection,
            [source],
            observed_at="2026-08-28T09:00:00+08:00",
        )
        self.assertEqual("ok", snapshot.tables[source.id].freshness["ingestion_update_time"].status)
        self.assertFalse(snapshot.errors)
        self.assertFalse(
            any("MAX(`ingestion_update_time`)" in sql for sql, _ in connection.cursor_obj.calls)
        )

    def test_enabled_key_quality_requires_a_freshness_bounded_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "unsafe_full_scan.yaml"
            source_path.write_text(
                """id: ywzt_ods.demo
source_id: doris_ywzt_ods
role: demo
business_key: id
fields:
  id: {type: integer}
metadata_sync:
  key_quality:
    enabled: true
    lookback_days: 30
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MetadataSyncError, "requires freshness.field to bound the scan"
            ):
                load_source_tables(directory)

    def test_snapshot_loader_accepts_flat_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flat.json"
            path.write_text(
                json.dumps(
                    {
                        "observed_at": "2026-08-28T09:00:00+08:00",
                        "columns": [
                            {"TABLE_SCHEMA": "ywzt_ods", "TABLE_NAME": "demo", "COLUMN_NAME": "id", "DATA_TYPE": "bigint"}
                        ],
                        "freshness": [
                            {"table_id": "ywzt_ods.demo", "field": "event_at", "max_value": "2026-08-28 08:00:00"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            snapshot = load_snapshot(path)
            self.assertEqual(snapshot.tables["ywzt_ods.demo"].columns["id"].data_type, "bigint")
            self.assertEqual(snapshot.tables["ywzt_ods.demo"].freshness["event_at"].status, "ok")

    def test_snapshot_key_quality_is_compatible_and_never_keeps_key_values(self) -> None:
        source = next(
            item
            for item in load_source_tables(ROOT / "sources" / "tables")
            if item.id == "ywzt_ods.scm_sales_shipment_header"
        )
        source = source.__class__(
            **{
                **source.__dict__,
                "id": "ywzt_ods.demo",
                "table": "demo",
                "business_key": "id",
                "freshness_field": "event_at",
                "freshness_config": {"field": "event_at"},
                "key_quality_enabled": True,
                "key_quality_lookback_days": 30,
                "key_quality_config": {"enabled": True, "lookback_days": 30},
                "fields": {
                    "id": {"type": "integer"},
                    "event_at": {"type": "datetime"},
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quality.json"
            path.write_text(
                json.dumps(
                    {
                        "observed_at": "2026-08-28T09:00:00+08:00",
                        "columns": [
                            {
                                "TABLE_SCHEMA": "ywzt_ods",
                                "TABLE_NAME": "demo",
                                "COLUMN_NAME": "id",
                                "DATA_TYPE": "bigint",
                            },
                            {
                                "TABLE_SCHEMA": "ywzt_ods",
                                "TABLE_NAME": "demo",
                                "COLUMN_NAME": "event_at",
                                "DATA_TYPE": "datetime",
                            },
                        ],
                        "freshness": [
                            {
                                "table_id": "ywzt_ods.demo",
                                "field": "event_at",
                                "max_value": "2026-08-28 08:59:00",
                            }
                        ],
                        "key_quality": [
                            {
                                "table_id": "ywzt_ods.demo",
                                "business_key": "id",
                                "lookback_days": 30,
                                "row_count": 10,
                                "distinct_count": 8,
                                "null_count": 1,
                                "duplicate_count": 1,
                                "sample_keys": ["DO-NOT-KEEP-THIS-KEY"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            snapshot = load_snapshot(path)
            report = compare_sources(
                [source],
                snapshot,
                now=datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc),
            )
            candidate = next(
                item for item in report.candidates if item.kind == "business_key_quality"
            )
            self.assertEqual(
                set(candidate.observed),
                {
                    "row_count",
                    "distinct_key_count",
                    "null_key_count",
                    "duplicate_key_count",
                },
            )
            serialized = json.dumps(report.to_dict(), ensure_ascii=False)
            self.assertNotIn("DO-NOT-KEEP-THIS-KEY", serialized)


if __name__ == "__main__":
    unittest.main()
