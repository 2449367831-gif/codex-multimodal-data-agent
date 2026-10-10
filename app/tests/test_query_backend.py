from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adhoc_sql import AdHocSqlService
from app.errors import QueryExecutionError
from app.executors import QueryExecutor
from app.models import ExecutionResult
from app.query_backend import create_query_backend, require_loopback_bind_host
from app.settings import RuntimeSettings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ApiExecutor(QueryExecutor):
    name = "doris"

    def health(self) -> dict[str, object]:
        return {"name": self.name, "ready": True}

    def execute(
        self,
        sql: str,
        bindings: dict[str, object],
        *,
        metric_id: str,
    ) -> ExecutionResult:
        return ExecutionResult(
            columns=("answer",),
            rows=({"answer": 1},),
            row_count=1,
            truncated=False,
            duration_ms=1,
            executor_name=self.name,
            is_mock=False,
        )


class QueryBackendApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="query-backend-api-")
        settings = RuntimeSettings(
            project_root=PROJECT_ROOT,
            executor_mode="doris",
            audit_db_path=Path(self.temp_dir.name) / "audit.sqlite3",
            doris_host="not-contacted.example",
            doris_user="readonly",
            doris_password="unused",
        )
        self.service = AdHocSqlService(settings, executor=ApiExecutor())
        self.client = TestClient(create_query_backend(self.service))

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def test_health_reports_sql_policy_and_no_row_audit(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["audit"]["stores_result_rows"])
        self.assertEqual("not_configured", payload["sql_policy"]["per_user_authorization"])
        self.assertFalse(payload["sql_policy"]["external_table_functions_allowed"])
        self.assertTrue(payload["sql_policy"]["row_expansion_functions_allowed"])
        self.assertEqual(64 * 1024 * 1024, payload["sql_policy"]["max_response_bytes"])
        self.assertEqual("connection_probe", payload["sql_policy"]["readiness_scope"])
        self.assertFalse(payload["sql_policy"]["readonly_privileges_verified"])
        self.assertFalse(payload["sql_policy"]["database_resource_limits_verified"])

    def test_non_loopback_clients_and_bind_hosts_are_rejected(self) -> None:
        remote = TestClient(
            create_query_backend(self.service),
            client=("192.0.2.10", 50000),
        )
        response = remote.get("/health")
        self.assertEqual(403, response.status_code)
        self.assertEqual("loopback_required", response.json()["error"]["code"])
        with self.assertRaises(RuntimeError):
            require_loopback_bind_host("0.0.0.0")
        self.assertEqual("::1", require_loopback_bind_host("::1"))

    def test_database_error_response_omits_private_message(self) -> None:
        self.service.executor.execute = MagicMock(
            side_effect=QueryExecutionError(
                "Doris query failed",
                details={
                    "error_type": "OperationalError",
                    "db_error_code": 2003,
                    "message": "cannot connect to secret-host.internal",
                },
            )
        )
        response = self.client.post("/v1/sql", json={"sql": "SELECT 1"})
        self.assertEqual(502, response.status_code)
        self.assertNotIn("secret-host.internal", response.text)
        self.assertNotIn("cannot connect", response.text)
        self.assertEqual(2003, response.json()["error"]["details"]["db_error_code"])

    def test_sql_endpoint_executes_select(self) -> None:
        response = self.client.post(
            "/v1/sql",
            json={"sql": "SELECT 1 AS answer", "purpose": "smoke test"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("succeeded", response.json()["status"])

    def test_sql_endpoint_rejects_unknown_fields_and_writes(self) -> None:
        unknown = self.client.post(
            "/v1/sql",
            json={"sql": "SELECT 1", "user_id": "model-claimed"},
        )
        self.assertEqual(422, unknown.status_code)
        self.assertEqual("invalid_parameters", unknown.json()["error"]["code"])

        write = self.client.post(
            "/v1/sql",
            json={"sql": "DELETE FROM ywzt_ods.example"},
        )
        self.assertEqual(409, write.status_code)
        self.assertEqual("sql_safety_error", write.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
