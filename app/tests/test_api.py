from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from fastapi.testclient import TestClient

from app.api import create_app
from app.errors import ContractNotApprovedError
from app.service import QueryService
from app.settings import RuntimeSettings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="query-agent-api-tests-")
        settings = RuntimeSettings(
            project_root=PROJECT_ROOT,
            executor_mode="mock",
            audit_db_path=Path(self.temp_dir.name) / "audit.sqlite3",
        )
        self.service = QueryService(settings)
        self.client = TestClient(
            create_app(
                self.service,
                legacy_enabled=True,
                enforce_loopback=True,
                expose_docs=False,
            )
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_health_and_metrics(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(200, health.status_code)
        health_payload = health.json()
        manifest = yaml.safe_load((PROJECT_ROOT / "releases/candidate-0.1.0.yaml").read_text(encoding="utf-8"))
        expected_ids = {ref.rsplit("@", 1)[0] for ref in manifest["contracts"]["metrics"]}
        self.assertEqual(len(expected_ids), health_payload["knowledge_base"]["metric_count"])
        self.assertNotIn("path", health_payload["audit"])
        self.assertNotIn("fixture_path", health_payload["executor"])
        metrics = self.client.get("/v1/metrics")
        self.assertEqual(len(expected_ids), metrics.json()["count"])
        self.assertEqual(expected_ids, {item["id"] for item in metrics.json()["items"]})

    def test_default_factory_disables_legacy_detail_surface(self) -> None:
        client = TestClient(create_app(self.service))
        self.assertEqual(200, client.get("/health").status_code)
        for path in (
            "/v1/metrics",
            "/v1/releases",
            "/v1/releases/current",
            "/v1/route",
            "/v1/query",
            "/v1/agent",
            "/v1/audits",
            "/docs",
            "/redoc",
            "/openapi.json",
        ):
            response = client.get(path)
            self.assertEqual(404, response.status_code, path)
            self.assertNotIn("sql", response.text.casefold(), path)

    def test_agent_plans_explicit_date_actual_outbound(self) -> None:
        response = self.client.post(
            "/v1/agent",
            json={
                "question": "查询2026年8月27日的实际销售出库量",
                "include_sql": True,
            },
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("planned", payload["action"])
        self.assertEqual("sales.outbound_actual", payload["plan"]["metric_id"])
        self.assertIn("%(start_inclusive)s", payload["plan"]["sql"])

    def test_unknown_agent_field_is_rejected(self) -> None:
        response = self.client.post(
            "/v1/agent", json={"question": "查询库存", "sql": "SELECT 1"}
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("invalid_parameters", response.json()["error"]["code"])

    def test_disabled_legacy_shell_hides_detail_routes_and_schema(self) -> None:
        client = TestClient(
            create_app(
                self.service,
                legacy_enabled=False,
                enforce_loopback=True,
                expose_docs=False,
            )
        )
        self.assertEqual(200, client.get("/health").status_code)
        for path in ("/v1/query", "/v1/agent", "/v1/audits", "/openapi.json", "/docs"):
            response = client.get(path)
            self.assertEqual(404, response.status_code, path)
            self.assertNotIn("sql", response.text.casefold())


class DorisApprovalGateTestCase(unittest.TestCase):
    def test_pending_contract_is_blocked_before_doris_connection(self) -> None:
        with TemporaryDirectory(prefix="query-agent-approval-tests-") as directory:
            service = QueryService(
                RuntimeSettings(
                    project_root=PROJECT_ROOT,
                    executor_mode="doris",
                    audit_db_path=Path(directory) / "audit.sqlite3",
                    doris_host="not-contacted.example",
                    doris_user="readonly",
                    doris_password="not-used",
                    doris_database="ywzt_ods",
                    allow_unapproved_contracts=False,
                )
            )
            with self.assertRaises(ContractNotApprovedError):
                service.query(
                    metric_id="sales.outbound_actual",
                    parameters={
                        "start_inclusive": "2026-08-27 00:00:00",
                        "end_exclusive": "2026-08-28 00:00:00",
                        "output_grain": "total",
                    },
                )


if __name__ == "__main__":
    unittest.main()
