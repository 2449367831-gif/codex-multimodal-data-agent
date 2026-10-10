from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from tools.sync_source_project import (
    SourceProjectSyncError,
    inspect_source_project,
    load_catalog,
    sync_source_project,
)


class SourceProjectSyncTests(unittest.TestCase):
    def _workspace(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
        holder = tempfile.TemporaryDirectory()
        root = Path(holder.name)
        source = root / "source"
        destination = root / "knowledge" / "source-project"
        source.mkdir(parents=True)
        destination.mkdir(parents=True)
        catalog = destination / "logic-catalog.yaml"
        catalog.write_text(
            yaml.safe_dump(
                {
                    "catalog_id": "test-catalog",
                    "catalog_version": "test-1",
                    "status": "imported_candidate",
                    "source_project": {
                        "primary_documents": [
                            "knowledge/source-project/docs/guide.md",
                        ],
                        "imported_script_files": [
                            "knowledge/source-project/scripts/job.py",
                        ],
                    },
                    "logic_families": [
                        {
                            "id": "test.query",
                            "source_files": [
                                "knowledge/source-project/sql/query.sql",
                                "knowledge/source-project/sql/deliverables/report.sql",
                            ],
                        }
                    ],
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return holder, source, destination, catalog

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_check_reports_hashes_missing_and_drift_without_writing(self) -> None:
        holder, source, destination, catalog = self._workspace()
        self.addCleanup(holder.cleanup)
        self._write(source / "docs/guide.md", "guide-v1\n")
        self._write(source / "scripts/job.py", "print('job')\n")
        self._write(source / "sql/query.sql", "SELECT 1\n")
        self._write(source / "deliverables/数仓Doris报表SQL包_20260824/sql/report.sql", "SELECT 2\n")
        self._write(destination / "docs/guide.md", "guide-v1\n")
        self._write(destination / "sql/query.sql", "SELECT old\n")
        self._write(destination / "README.md", "project metadata\n")
        before = (destination / "sql/query.sql").read_bytes()

        report = inspect_source_project(source, destination, catalog, mode="check")
        self.assertEqual(report["mode"], "check")
        self.assertEqual(report["summary"]["managed_file_count"], 4)
        self.assertEqual(report["summary"]["clean"], 1)
        self.assertEqual(report["summary"]["drift"], 1)
        self.assertEqual(report["summary"]["missing_destination"], 2)
        self.assertIn("README.md", report["extra_destination_files"])
        self.assertTrue(report["summary"]["has_drift"])
        rows = {row["destination"]: row for row in report["files"]}
        self.assertEqual(len(rows["docs/guide.md"]["source_sha256"]), 64)
        self.assertEqual(rows["sql/query.sql"]["status"], "drift")
        self.assertEqual((destination / "sql/query.sql").read_bytes(), before)

    def test_sync_copies_only_catalog_files_and_leaves_contracts_alone(self) -> None:
        holder, source, destination, catalog = self._workspace()
        self.addCleanup(holder.cleanup)
        self._write(source / "docs/guide.md", "new guide\n")
        self._write(source / "scripts/job.py", "print('new job')\n")
        self._write(source / "sql/query.sql", "SELECT 42\n")
        self._write(source / "deliverables/数仓Doris报表SQL包_20260824/sql/report.sql", "SELECT 43\n")
        self._write(destination / "docs/guide.md", "old guide\n")
        self._write(destination / "metrics-not-managed.yaml", "keep me\n")

        preflight = inspect_source_project(source, destination, catalog, mode="sync")
        self.assertFalse(preflight["summary"]["sync_blocked"])
        result = sync_source_project(preflight)
        self.assertEqual(result["summary"]["sync_action_count"], 4)
        self.assertTrue(all(row["status"] == "clean" for row in result["files"]))
        self.assertEqual((destination / "docs/guide.md").read_text(encoding="utf-8"), "new guide\n")
        self.assertEqual(
            (destination / "sql/deliverables/report.sql").read_text(encoding="utf-8"),
            "SELECT 43\n",
        )
        self.assertEqual((destination / "metrics-not-managed.yaml").read_text(encoding="utf-8"), "keep me\n")
        self.assertFalse((destination / "logic-catalog.yaml").stat().st_size == 0)

    def test_secret_pattern_blocks_sync_and_does_not_copy(self) -> None:
        holder, source, destination, catalog = self._workspace()
        self.addCleanup(holder.cleanup)
        self._write(source / "docs/guide.md", "guide\n")
        self._write(source / "scripts/job.py", "OPENAI_API_KEY = 'sk-123456789012345678901234'\n")
        self._write(source / "sql/query.sql", "SELECT 1\n")
        self._write(source / "deliverables/数仓Doris报表SQL包_20260824/sql/report.sql", "SELECT 2\n")

        report = inspect_source_project(source, destination, catalog, mode="sync")
        self.assertEqual(report["summary"]["blocked"], 1)
        blocked = next(row for row in report["files"] if row["status"] == "blocked")
        self.assertIn("openai_api_key", blocked["source_error"])
        with self.assertRaises(SourceProjectSyncError):
            sync_source_project(report)
        self.assertFalse((destination / "scripts/job.py").exists())

    def test_catalog_rejects_backslashes_and_forbidden_components(self) -> None:
        holder, source, destination, catalog = self._workspace()
        self.addCleanup(holder.cleanup)
        data = yaml.safe_load(catalog.read_text(encoding="utf-8"))
        data["source_project"]["primary_documents"] = ["knowledge\\source-project\\docs\\guide.md"]
        catalog.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(SourceProjectSyncError):
            load_catalog(catalog)

    def test_catalog_scope_count_mismatch_blocks_sync(self) -> None:
        holder, source, destination, catalog = self._workspace()
        self.addCleanup(holder.cleanup)
        self._write(source / "docs/guide.md", "guide\n")
        self._write(source / "scripts/job.py", "print('job')\n")
        self._write(source / "sql/query.sql", "SELECT 1\n")
        self._write(source / "deliverables/数仓Doris报表SQL包_20260824/sql/report.sql", "SELECT 2\n")
        data = yaml.safe_load(catalog.read_text(encoding="utf-8"))
        data["source_project"]["imported_scope"] = {
            "imported_core_sql_files": 99,
            "imported_deliverable_sql_files": 1,
            "doris_query_scripts": 2,
        }
        catalog.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        report = inspect_source_project(source, destination, catalog, mode="sync")
        self.assertEqual(report["summary"]["scope_count_mismatch_count"], 2)
        self.assertTrue(report["summary"]["sync_blocked"])
        with self.assertRaises(SourceProjectSyncError):
            sync_source_project(report)

        data["source_project"]["primary_documents"] = ["knowledge/source-project/build/guide.md"]
        catalog.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(SourceProjectSyncError):
            load_catalog(catalog)

    def test_report_is_json_serializable_and_main_output_can_be_consumed(self) -> None:
        holder, source, destination, catalog = self._workspace()
        self.addCleanup(holder.cleanup)
        self._write(source / "docs/guide.md", "guide\n")
        self._write(source / "scripts/job.py", "print('job')\n")
        self._write(source / "sql/query.sql", "SELECT 1\n")
        self._write(source / "deliverables/数仓Doris报表SQL包_20260824/sql/report.sql", "SELECT 2\n")
        report = inspect_source_project(source, destination, catalog)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertIn('"source_sha256"', encoded)
        self.assertIn('"missing_destination"', encoded)
        # Keep stdout behavior obvious to callers without invoking a real source
        # path or writing a report file in the repository.
        from tools.sync_source_project import main

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--source-root",
                    str(source),
                    "--destination-root",
                    str(destination),
                    "--catalog",
                    str(catalog),
                ]
            )
        self.assertEqual(exit_code, 1)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["schema_version"], "1.0.0")

    def test_unlisted_source_files_are_reported_without_being_read_or_copied(self) -> None:
        holder, source, destination, catalog = self._workspace()
        self.addCleanup(holder.cleanup)
        self._write(source / "docs/guide.md", "guide\n")
        self._write(source / "scripts/job.py", "print('job')\n")
        self._write(source / "sql/query.sql", "SELECT 1\n")
        self._write(source / "deliverables/数仓Doris报表SQL包_20260824/sql/report.sql", "SELECT 2\n")
        self._write(source / "sql/new_business_variant.sql", "SELECT 3\n")
        # A scratch/output tree is intentionally outside the review inventory.
        self._write(source / "outputs/generated.sql", "SELECT 4\n")

        report = inspect_source_project(source, destination, catalog)
        self.assertEqual(report["summary"]["unlisted_source_count"], 1)
        self.assertEqual(report["unlisted_source_files"], ["sql/new_business_variant.sql"])
        self.assertEqual(report["catalog_scope"]["actual_allowlist"]["imported_sql_files"], 2)
        self.assertEqual(report["catalog_scope"]["actual_allowlist"]["known_unlisted_sql_files"], 1)
        self.assertFalse(report["source_scan"]["content_read"])
        self.assertFalse(report["summary"]["sync_blocked"])

        from tools.sync_source_project import main

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--source-root",
                    str(source),
                    "--destination-root",
                    str(destination),
                    "--catalog",
                    str(catalog),
                    "--fail-on-unlisted-source",
                ]
            )
        self.assertEqual(exit_code, 1)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["summary"]["unlisted_source_count"], 1)


if __name__ == "__main__":
    unittest.main()
