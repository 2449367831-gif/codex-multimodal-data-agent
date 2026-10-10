from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import os
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

from app.audit import AuditStore
from app.api import create_app
from app.errors import ContractNotApprovedError, KnowledgeBaseError, MetricNotFoundError
from app.executors import MockExecutor, QueryExecutor
from app.knowledge import KnowledgeBase
from app.models import ExecutionResult
from app.release_integrity import build_manifest_integrity, manifest_integrity_errors
from app.service import QueryService
from app.settings import RuntimeSettings
from app.sql import TemplateCompiler
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RecordingDorisExecutor(QueryExecutor):
    name = "doris"

    def __init__(self) -> None:
        self.calls = 0
        self.sql_calls: list[str] = []

    def execute(self, sql: str, bindings: dict[str, object], *, metric_id: str) -> ExecutionResult:
        self.calls += 1
        self.sql_calls.append(sql)
        return ExecutionResult(
            columns=("value",),
            rows=(({"value": 1}),),
            row_count=1,
            truncated=False,
            duration_ms=0,
            executor_name=self.name,
            is_mock=False,
        )


def _copy_runtime_project(target: Path) -> None:
    for name in ("app", "metrics", "sources", "profiles", "rules", "evals", "sql_templates"):
        shutil.copytree(PROJECT_ROOT / name, target / name)
    shutil.copytree(
        PROJECT_ROOT / "tests" / "golden_questions",
        target / "tests" / "golden_questions",
    )
    (target / "releases").mkdir()


def _make_released_project(target: Path, *, metric_ref: str | None = None) -> None:
    _copy_runtime_project(target)
    metrics_dir = target / "metrics"
    for path in metrics_dir.glob("*.yaml"):
        metric = yaml.safe_load(path.read_text(encoding="utf-8"))
        metric["status"] = "active"
        metric.setdefault("approval", {})["state"] = "approved"
        metric["approval"]["business_owner"] = "test-owner"
        metric["approval"]["approved_by"] = "test-owner"
        metric["approval"]["approved_at"] = "2026-08-28T09:00:00+08:00"
        if metric["id"] == "sales.outbound_actual":
            metric["required_validations"] = ["single_statement_and_read_only"]
        path.write_text(yaml.safe_dump(metric, allow_unicode=True, sort_keys=False), encoding="utf-8")

    candidate = yaml.safe_load(
        (PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml").read_text(encoding="utf-8")
    )
    candidate.update(
        {
            "release_id": "release-0.1.0",
            "knowledge_version": "0.1.0",
            "status": "released",
            "released_at": "2026-08-28T09:00:00+08:00",
        }
    )
    candidate["approval"] = {
        "state": "approved",
        "approved_by": "test-owner",
        "approved_at": "2026-08-28T09:00:00+08:00",
    }
    if metric_ref is not None:
        candidate["contracts"]["metrics"] = [metric_ref]
    candidate["integrity"] = build_manifest_integrity(target, candidate)
    (target / "releases" / "release-0.1.0.yaml").write_text(
        yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _refresh_release_integrity(root: Path, release_id: str = "release-0.1.0") -> Path:
    manifest_path = root / "releases" / f"{release_id}.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["integrity"] = build_manifest_integrity(root, manifest)
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return manifest_path


def _sales_parameters() -> dict[str, object]:
    return {
        "start_inclusive": "2026-08-27 00:00:00",
        "end_exclusive": "2026-08-28 00:00:00",
        "output_grain": "total",
    }


class KnowledgeBaseReleaseTestCase(unittest.TestCase):
    def test_settings_support_external_contract_repository_and_var_audit(self) -> None:
        with TemporaryDirectory(prefix="query-agent-settings-") as directory:
            root = Path(directory).resolve()
            with patch.dict(
                os.environ,
                {
                    "AGENT_PROJECT_ROOT": str(root),
                    "AGENT_TRUSTED_RELEASE_SHA256": "A" * 64,
                },
                clear=True,
            ):
                settings = RuntimeSettings.from_env()
            self.assertEqual(root, settings.project_root)
            self.assertEqual(root / "var" / "audit.sqlite3", settings.audit_db_path)
            self.assertEqual("a" * 64, settings.trusted_release_sha256)

    def test_candidate_is_used_for_plans_but_not_production_release(self) -> None:
        knowledge = KnowledgeBase(PROJECT_ROOT)
        self.assertEqual("candidate-0.1.0", knowledge.planning_manifest()["release_id"])
        self.assertIsNone(knowledge.released_manifest())
        release_id, source_versions = knowledge.plan_release_context(
            metric_id="sales.outbound_actual",
            metric_version="2.0.0",
            source_ids=("ywzt_ods.scm_sales_shipment_header",),
        )
        self.assertEqual("candidate-0.1.0", release_id)
        self.assertEqual("1.0.0", source_versions["ywzt_ods.scm_sales_shipment_header"])

    def test_candidate_integrity_is_deterministic_and_valid(self) -> None:
        manifest = yaml.safe_load(
            (PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual([], manifest_integrity_errors(PROJECT_ROOT, manifest))
        self.assertEqual(
            build_manifest_integrity(PROJECT_ROOT, manifest),
            build_manifest_integrity(PROJECT_ROOT, manifest),
        )

    def test_integrity_selects_exact_golden_suite_version(self) -> None:
        with TemporaryDirectory(prefix="query-agent-golden-release-") as directory:
            root = Path(directory).resolve()
            _copy_runtime_project(root)
            alternate_path = root / "tests" / "golden_questions" / "core-routing-v1.3.yaml"
            alternate = yaml.safe_load(
                (root / "tests" / "golden_questions" / "core-routing.yaml").read_text(
                    encoding="utf-8"
                )
            )
            alternate["version"] = "1.3.0"
            alternate_path.write_text(
                yaml.safe_dump(alternate, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            manifest = yaml.safe_load(
                (PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml").read_text(
                    encoding="utf-8"
                )
            )

            files = build_manifest_integrity(root, manifest)["files"]

            self.assertIn("tests/golden_questions/core-routing.yaml", files)
            self.assertNotIn("tests/golden_questions/core-routing-v1.3.yaml", files)

    def test_schema_requires_integrity_only_after_release(self) -> None:
        schema = json.loads(
            (PROJECT_ROOT / "schemas" / "release-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = yaml.safe_load(
            (PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml").read_text(encoding="utf-8")
        )
        manifest.pop("integrity", None)
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(manifest)))
        manifest["status"] = "released"
        errors = list(Draft202012Validator(schema).iter_errors(manifest))
        self.assertTrue(any("integrity" in error.message for error in errors))

    def test_integrity_requires_every_metric_declared_source(self) -> None:
        manifest = yaml.safe_load(
            (PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml").read_text(encoding="utf-8")
        )
        manifest["contracts"]["sources"].remove(
            "ywzt_ods.scm_sales_shipment_header@1.0.0"
        )
        errors = manifest_integrity_errors(PROJECT_ROOT, manifest)
        self.assertTrue(any("omits source contracts" in error for error in errors))

    def test_candidate_manifest_pins_v2_when_v2_1_contract_also_exists(self) -> None:
        with TemporaryDirectory(prefix="query-agent-multiversion-") as directory:
            root = Path(directory)
            _copy_runtime_project(root)
            shutil.copy(PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml", root / "releases")
            original = root / "metrics" / "sales.outbound_actual.v2.yaml"
            v2_1 = yaml.safe_load(original.read_text(encoding="utf-8"))
            v2_1["version"] = "2.1.0"
            v2_1["status"] = "active"
            (root / "metrics" / "sales.outbound_actual.v2.1.0.yaml").write_text(
                yaml.safe_dump(v2_1, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )

            service = QueryService(
                RuntimeSettings(
                    project_root=root,
                    executor_mode="mock",
                    audit_db_path=root / "audit.sqlite3",
                )
            )
            self.assertEqual("2.0.0", service.get_metric("sales.outbound_actual")["version"])
            self.assertEqual(
                "2.1.0", service.knowledge.metric("sales.outbound_actual", "2.1.0")["version"]
            )
            plan = service.build_plan(
                metric_id="sales.outbound_actual", parameters=_sales_parameters()
            )
            self.assertEqual("2.0.0", plan.metric_version)
            self.assertEqual("candidate-0.1.0", plan.release_id)

    def test_without_manifest_prefers_unique_active_then_highest_version(self) -> None:
        with TemporaryDirectory(prefix="query-agent-version-fallback-") as directory:
            root = Path(directory)
            _copy_runtime_project(root)
            original = root / "metrics" / "sales.outbound_actual.v2.yaml"
            v2 = yaml.safe_load(original.read_text(encoding="utf-8"))
            v2["status"] = "active"
            original.write_text(
                yaml.safe_dump(v2, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            v2_1 = dict(v2)
            v2_1["version"] = "2.1.0"
            v2_1["status"] = "candidate"
            newer = root / "metrics" / "sales.outbound_actual.v2.1.0.yaml"
            newer.write_text(
                yaml.safe_dump(v2_1, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            self.assertEqual("2.0.0", KnowledgeBase(root).metric("sales.outbound_actual")["version"])

            v2["status"] = "candidate"
            original.write_text(
                yaml.safe_dump(v2, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            self.assertEqual("2.1.0", KnowledgeBase(root).metric("sales.outbound_actual")["version"])

    def test_explicit_release_id_selects_manifest(self) -> None:
        with patch.dict(os.environ, {"AGENT_RELEASE_ID": "candidate-0.1.0"}, clear=False):
            knowledge = KnowledgeBase(PROJECT_ROOT)
        self.assertEqual("candidate-0.1.0", knowledge.planning_manifest()["release_id"])
        self.assertIsNone(knowledge.released_manifest())

    def test_multiple_released_manifests_require_explicit_selection(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-kb-") as directory:
            root = Path(directory)
            _copy_runtime_project(root)
            release_dir = root / "releases"
            base = yaml.safe_load(
                (PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml").read_text(encoding="utf-8")
            )
            for name in ("a", "b"):
                manifest = dict(base)
                manifest.update(
                    {
                        "release_id": f"release-{name}",
                        "status": "released",
                        "released_at": "2026-08-28T09:00:00+08:00",
                    }
                )
                manifest["approval"] = {
                    "state": "approved",
                    "approved_by": "owner",
                    "approved_at": "2026-08-28T09:00:00+08:00",
                }
                (release_dir / f"{name}.yaml").write_text(
                    yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
                )
            with self.assertRaises(KnowledgeBaseError):
                KnowledgeBase(root)

    def test_current_release_api_exposes_released_and_planning_manifests(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-api-") as directory:
            root = Path(directory)
            _make_released_project(root)
            service = QueryService(
                RuntimeSettings(
                    project_root=root,
                    executor_mode="mock",
                    audit_db_path=root / "audit.sqlite3",
                )
            )
            client = TestClient(
                create_app(
                    service,
                    legacy_enabled=True,
                    enforce_loopback=True,
                    expose_docs=False,
                )
            )
            response = client.get("/v1/releases/current")
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual("release-0.1.0", body["manifest"]["release_id"])
            self.assertEqual("release-0.1.0", body["planning_manifest"]["release_id"])


class DorisReleaseGateTestCase(unittest.TestCase):
    def _service(self, root: Path, *, allow_override: bool = False) -> tuple[QueryService, RecordingDorisExecutor]:
        executor = RecordingDorisExecutor()
        released_paths = []
        for path in (root / "releases").glob("*.yaml"):
            manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
            if manifest.get("status") == "released":
                released_paths.append(path)
        trusted_digest = (
            sha256(released_paths[0].read_bytes()).hexdigest()
            if len(released_paths) == 1
            else None
        )
        service = QueryService(
            RuntimeSettings(
                project_root=root,
                executor_mode="doris",
                audit_db_path=root / "audit.sqlite3",
                allow_unapproved_contracts=allow_override,
                trusted_release_sha256=trusted_digest,
            ),
            executor=executor,
        )
        return service, executor

    def test_exact_released_metric_version_can_execute(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-execute-") as directory:
            root = Path(directory)
            _make_released_project(root)
            service, executor = self._service(root)
            result = service.query(metric_id="sales.outbound_actual", parameters=_sales_parameters())
            self.assertIn(result["status"], {"succeeded", "incomplete"})
            self.assertEqual(1, executor.calls)
            self.assertEqual("release-0.1.0", result["release_id"])
            self.assertEqual("1.0.0", result["source_versions"]["ywzt_ods.scm_sales_shipment_header"])
            audit = service.audit.get(result["request_id"])
            self.assertEqual("release-0.1.0", audit["release_id"])
            self.assertIn("ywzt_ods.scm_sales_shipment_detail", audit["source_versions"])

    def test_actual_injected_executor_controls_runtime_freezing(self) -> None:
        with TemporaryDirectory(prefix="query-agent-actual-executor-") as directory:
            root = Path(directory)
            _make_released_project(root)
            manifest_path = root / "releases" / "release-0.1.0.yaml"
            executor = RecordingDorisExecutor()
            service = QueryService(
                RuntimeSettings(
                    project_root=root,
                    executor_mode="mock",
                    audit_db_path=root / "audit.sqlite3",
                    trusted_release_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
                ),
                executor=executor,
            )
            self.assertFalse(service.knowledge.reload_on_change)
            self.assertFalse(service.compiler.reload_on_change)

            template_path = root / "sql_templates" / "sales.outbound_actual.v2.sql"
            template_path.write_text(
                template_path.read_text(encoding="utf-8") + "\n-- post-startup tamper\n",
                encoding="utf-8",
            )
            service.query(metric_id="sales.outbound_actual", parameters=_sales_parameters())
            self.assertEqual(1, executor.calls)
            self.assertNotIn("post-startup tamper", executor.sql_calls[0])
            _refresh_release_integrity(root)

            mock_service = QueryService(
                RuntimeSettings(
                    project_root=root,
                    executor_mode="doris",
                    audit_db_path=root / "mock-audit.sqlite3",
                ),
                executor=MockExecutor(None, max_rows=10),
            )
            self.assertTrue(mock_service.knowledge.reload_on_change)
            self.assertTrue(mock_service.compiler.reload_on_change)

    def test_cached_template_digest_blocks_change_then_restore_attack(self) -> None:
        with TemporaryDirectory(prefix="query-agent-cache-digest-") as directory:
            root = Path(directory)
            _make_released_project(root)
            manifest_path = root / "releases" / "release-0.1.0.yaml"
            knowledge = KnowledgeBase(
                root,
                reload_on_change=False,
                require_trusted_release=True,
                trusted_release_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
            )
            template_path = root / "sql_templates" / "sales.outbound_actual.v2.sql"
            original = template_path.read_bytes()
            template_path.write_bytes(original + b"\n-- transient tamper\n")
            compiler = TemplateCompiler(
                root,
                reload_on_change=False,
                preload_template_refs=knowledge.selected_template_refs(),
            )
            template_path.write_bytes(original)
            with self.assertRaises(KnowledgeBaseError) as raised:
                knowledge.verify_runtime_snapshot_integrity(
                    compiler.cached_file_digests()
                )
            self.assertIn("runtime bytes", str(raised.exception).casefold())

    def test_pending_blocking_validation_is_blocked_before_executor(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-validation-") as directory:
            root = Path(directory)
            _make_released_project(root)
            metric_path = root / "metrics" / "sales.outbound_actual.v2.yaml"
            metric = yaml.safe_load(metric_path.read_text(encoding="utf-8"))
            metric["required_validations"].append("source_freshness")
            metric_path.write_text(
                yaml.safe_dump(metric, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            _refresh_release_integrity(root)
            service, executor = self._service(root)
            with self.assertRaises(ContractNotApprovedError) as raised:
                service.query(metric_id="sales.outbound_actual", parameters=_sales_parameters())
            self.assertEqual(0, executor.calls)
            self.assertIn(
                "source_freshness",
                raised.exception.details["pending_blocking_validations"],
            )

    def test_metric_missing_from_release_is_not_exposed_to_executor(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-mismatch-") as directory:
            root = Path(directory)
            _make_released_project(root, metric_ref="inventory.available_realtime@1.0.0")
            service, executor = self._service(root)
            with self.assertRaises(MetricNotFoundError):
                service.query(metric_id="sales.outbound_actual", parameters=_sales_parameters())
            self.assertFalse(executor.calls)
            record = service.audit.list(limit=1)[0]
            self.assertEqual("blocked", record["status"])
            self.assertIsNone(record["release_id"])

    def test_release_manifest_pins_source_when_newer_version_exists(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-source-version-") as directory:
            root = Path(directory)
            _make_released_project(root)
            source_path = root / "sources" / "tables" / "scm_sales_shipment_header.yaml"
            source = yaml.safe_load(source_path.read_text(encoding="utf-8"))
            source["version"] = "2.0.0"
            (root / "sources" / "tables" / "scm_sales_shipment_header.v2.yaml").write_text(
                yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            service, executor = self._service(root)
            result = service.query(
                metric_id="sales.outbound_actual", parameters=_sales_parameters()
            )
            self.assertEqual(1, executor.calls)
            self.assertEqual(
                "1.0.0", result["source_versions"]["ywzt_ods.scm_sales_shipment_header"]
            )

    def test_released_contract_tamper_fails_closed_at_startup(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-tamper-") as directory:
            root = Path(directory)
            _make_released_project(root)
            template_path = root / "sql_templates" / "sales.outbound_actual.v2.sql"
            template_path.write_text(
                template_path.read_text(encoding="utf-8") + "\n-- same-version tamper\n",
                encoding="utf-8",
            )
            with self.assertRaises(KnowledgeBaseError) as raised:
                self._service(root)
            self.assertIn("integrity", str(raised.exception).casefold())

    def test_released_manifest_without_integrity_fails_closed_at_startup(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-integrity-missing-") as directory:
            root = Path(directory)
            _make_released_project(root)
            manifest_path = root / "releases" / "release-0.1.0.yaml"
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("integrity")
            manifest_path.write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaises(KnowledgeBaseError):
                self._service(root)

    def test_released_manifest_requires_external_trust_anchor_in_doris_mode(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-anchor-required-") as directory:
            root = Path(directory)
            _make_released_project(root)
            with self.assertRaises(KnowledgeBaseError) as raised:
                QueryService(
                    RuntimeSettings(
                        project_root=root,
                        executor_mode="doris",
                        audit_db_path=root / "audit.sqlite3",
                    ),
                    executor=RecordingDorisExecutor(),
                )
            self.assertIn("AGENT_TRUSTED_RELEASE_SHA256", str(raised.exception))

    def test_manifest_rewrite_fails_external_trust_anchor(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-anchor-mismatch-") as directory:
            root = Path(directory)
            _make_released_project(root)
            manifest_path = root / "releases" / "release-0.1.0.yaml"
            trusted_before = sha256(manifest_path.read_bytes()).hexdigest()
            metric_path = root / "metrics" / "sales.outbound_actual.v2.yaml"
            metric = yaml.safe_load(metric_path.read_text(encoding="utf-8"))
            metric["notes"] = str(metric.get("notes", "")) + " changed with manifest"
            metric_path.write_text(
                yaml.safe_dump(metric, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            _refresh_release_integrity(root)
            with self.assertRaises(KnowledgeBaseError) as raised:
                QueryService(
                    RuntimeSettings(
                        project_root=root,
                        executor_mode="doris",
                        audit_db_path=root / "audit.sqlite3",
                        trusted_release_sha256=trusted_before,
                    ),
                    executor=RecordingDorisExecutor(),
                )
            self.assertIn("manifest SHA-256 mismatch", str(raised.exception))

    def test_preflight_routes_then_blocks_pending_contract_once(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-preflight-") as directory:
            executor = RecordingDorisExecutor()
            service = QueryService(
                RuntimeSettings(
                    project_root=PROJECT_ROOT,
                    executor_mode="doris",
                    audit_db_path=Path(directory) / "audit.sqlite3",
                ),
                executor=executor,
            )
            with self.assertRaises(ContractNotApprovedError):
                service.query(
                    metric_id=None,
                    question="查询实际销售出库量",
                    parameters=_sales_parameters(),
                )
            self.assertFalse(executor.calls)
            records = service.audit.list(limit=10)
            self.assertEqual(1, len(records))
            self.assertEqual("sales.outbound_actual", records[0]["metric_id"])
            self.assertEqual("blocked", records[0]["status"])

    def test_explicit_override_allows_pre_release_smoke_and_records_candidate(self) -> None:
        with TemporaryDirectory(prefix="query-agent-release-override-") as directory:
            root = Path(directory)
            _copy_runtime_project(root)
            shutil.copy(PROJECT_ROOT / "releases" / "candidate-0.1.0.yaml", root / "releases")
            service, executor = self._service(root, allow_override=True)
            result = service.query(metric_id="sales.outbound_actual", parameters=_sales_parameters())
            self.assertIn(result["status"], {"succeeded", "incomplete"})
            self.assertEqual(1, executor.calls)
            self.assertEqual("candidate-0.1.0", result["release_id"])


class AuditMigrationTestCase(unittest.TestCase):
    def test_old_sqlite_schema_is_migrated_and_decoded(self) -> None:
        with TemporaryDirectory(prefix="query-agent-audit-migration-") as directory:
            path = Path(directory) / "audit.sqlite3"
            import sqlite3

            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE query_audit (
                    request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, metric_id TEXT, metric_version TEXT, question TEXT,
                    parameters_json TEXT NOT NULL, sql_text TEXT, bindings_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL, executor_name TEXT, duration_ms INTEGER,
                    row_count INTEGER, truncated INTEGER, is_mock INTEGER, error_json TEXT
                )
                """
            )
            connection.commit()
            connection.close()

            store = AuditStore(path)
            store.upsert(
                {
                    "request_id": "migration-test",
                    "created_at": "2026-08-28T09:00:00+08:00",
                    "updated_at": "2026-08-28T09:00:00+08:00",
                    "status": "planned",
                    "metric_id": "sales.outbound_actual",
                    "metric_version": "1.0.0",
                    "release_id": "candidate-0.1.0",
                    "source_versions": {"source": "1.0.0"},
                    "parameters": {},
                    "bindings": {},
                    "validation_results": [],
                }
            )
            record = store.get("migration-test")
            self.assertEqual("candidate-0.1.0", record["release_id"])
            self.assertEqual({"source": "1.0.0"}, record["source_versions"])


if __name__ == "__main__":
    unittest.main()
