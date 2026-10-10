from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from app.adhoc_sql import AdHocSqlService, AdHocSqlValidator
from app.audit import AuditStore
from app.errors import ExecutorUnavailableError, SqlSafetyError
from app.executors import DorisExecutor, QueryExecutor
from app.models import ExecutionResult
from app.settings import RuntimeSettings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RecordingExecutor(QueryExecutor):
    name = "doris"

    def __init__(self, *, rows: tuple[dict[str, object], ...] = ({"answer": 1},)) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def health(self) -> dict[str, object]:
        return {"name": self.name, "ready": True}

    def execute(
        self,
        sql: str,
        bindings: dict[str, object],
        *,
        metric_id: str,
    ) -> ExecutionResult:
        self.calls.append((sql, bindings, metric_id))
        return ExecutionResult(
            columns=tuple(self.rows[0]) if self.rows else (),
            rows=self.rows,
            row_count=len(self.rows),
            truncated=False,
            duration_ms=3,
            executor_name=self.name,
            is_mock=False,
        )


class NonDorisExecutor(RecordingExecutor):
    name = "mock"


class AdHocSqlValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = AdHocSqlValidator(
            allowed_schemas=("ywzt_ods", "information_schema"),
            allowed_catalogs=("internal",),
            max_rows=10,
        )

    def test_select_cte_union_and_metadata_queries_are_allowed(self) -> None:
        cases = (
            "SELECT 1",
            "WITH x AS (SELECT * FROM ywzt_ods.stock) SELECT * FROM x",
            "SELECT * FROM ywzt_ods.a UNION ALL SELECT * FROM ywzt_ods.b",
            "SELECT table_name FROM information_schema.tables LIMIT 2",
            "SELECT * FROM internal.ywzt_ods.stock LIMIT 10 OFFSET 2",
            # Row-expansion functions are intentionally allowed; external TVFs are not.
            "SELECT * FROM UNNEST(CAST(NULL AS ARRAY<INT>)) AS values_table(value)",
            "SELECT * FROM ywzt_ods.stock LATERAL VIEW EXPLODE_SPLIT(code, ',') v AS value",
        )
        for sql in cases:
            with self.subTest(sql=sql):
                validated = self.validator.validate(sql)
                self.assertIn("LIMIT", validated.sql.upper())

    def test_limit_is_injected_from_validated_ast(self) -> None:
        validated = self.validator.validate(
            "WITH x AS (SELECT * FROM ywzt_ods.stock) SELECT * FROM x"
        )
        self.assertEqual(
            "WITH x AS (SELECT * FROM ywzt_ods.stock) SELECT * FROM x LIMIT 11",
            validated.sql,
        )
        self.assertEqual(("ywzt_ods.stock",), validated.source_tables)

    def test_cte_scope_shadow_does_not_hide_unqualified_outer_table(self) -> None:
        sql = (
            "SELECT * FROM secret_table WHERE EXISTS ("
            "WITH secret_table AS (SELECT * FROM ywzt_ods.allowed_table) "
            "SELECT 1 FROM secret_table)"
        )
        with self.assertRaises(SqlSafetyError) as raised:
            self.validator.validate(sql)
        self.assertIn("fully qualified", raised.exception.message)

    def test_dangerous_or_side_effecting_constructs_are_blocked(self) -> None:
        cases = (
            "INSERT INTO ywzt_ods.t VALUES (1)",
            "SELECT 1; SELECT 2",
            "SELECT * FROM ywzt_ods.t FOR UPDATE",
            "SELECT * FROM FILES(\"uri\"=\"s3://bucket/x\")",
            "SELECT SLEEP(1)",
            "SELECT SLEEP_MS(1)",
            "SELECT AI_GENERATE('x')",
            "SELECT IS_FREE_LOCK('x')",
            "SELECT RELEASE_ALL_LOCKS()",
            "SELECT @x := 1",
            "SELECT @@global.version",
            "WITH RECURSIVE t AS (SELECT 1 UNION ALL SELECT 1 FROM t) SELECT * FROM t",
            (
                "SELECT * FROM ywzt_ods.allowed_table WHERE EXISTS ("
                "WITH RECURSIVE r AS (SELECT 1 UNION ALL SELECT 1 FROM r) "
                "SELECT * FROM r)"
            ),
        )
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(SqlSafetyError):
                self.validator.validate(sql)

    def test_unqualified_cross_schema_catalog_and_excessive_limit_are_blocked(self) -> None:
        cases = (
            "SELECT * FROM stock",
            "SELECT * FROM secret.stock",
            "SELECT * FROM external.ywzt_ods.stock",
            "SELECT * FROM ywzt_ods.stock LIMIT 12",
            "SELECT * FROM ywzt_ods.stock LIMIT @n",
        )
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(SqlSafetyError):
                self.validator.validate(sql)


class AdHocSqlServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory(prefix="adhoc-sql-service-")
        self.settings = RuntimeSettings(
            project_root=PROJECT_ROOT,
            executor_mode="doris",
            audit_db_path=Path(self.temp_dir.name) / "audit.sqlite3",
            doris_host="not-contacted.example",
            doris_user="readonly",
            doris_password="unused",
            sql_allowed_schemas=("ywzt_ods", "information_schema"),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_service_executes_validated_sql_and_audits_without_rows(self) -> None:
        executor = RecordingExecutor()
        service = AdHocSqlService(self.settings, executor=executor)
        try:
            response = service.query(
                sql="SELECT COUNT(*) AS answer FROM ywzt_ods.example",
                purpose="count rows",
            )
            self.assertEqual("succeeded", response["status"])
            self.assertEqual(1, response["execution"]["row_count"])
            self.assertEqual(1, len(executor.calls))
            audit = service.audit.get(response["request_id"])
            self.assertEqual("succeeded", audit["status"])
            self.assertNotIn("rows", audit)
        finally:
            service.close()

    def test_service_blocks_non_doris_executor_before_execution(self) -> None:
        executor = NonDorisExecutor()
        service = AdHocSqlService(self.settings, executor=executor)
        try:
            with self.assertRaises(ExecutorUnavailableError):
                service.query(sql="SELECT 1")
            self.assertEqual([], executor.calls)
        finally:
            service.close()

    def test_invalid_sql_is_audited_as_blocked(self) -> None:
        service = AdHocSqlService(self.settings, executor=RecordingExecutor())
        try:
            with self.assertRaises(SqlSafetyError):
                service.query(sql="DROP TABLE ywzt_ods.example")
            self.assertEqual("blocked", service.audit.list(limit=1)[0]["status"])
        finally:
            service.close()


class DorisExecutorTestCase(unittest.TestCase):
    @patch("pymysql.connect")
    def test_health_probes_connection_and_caches_success(self, connect: MagicMock) -> None:
        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (1,)
        executor = DorisExecutor(
            host="127.0.0.1",
            port=9030,
            user="readonly",
            password="unused",
            database="ywzt_ods",
            timeout_seconds=60,
            max_rows=10,
        )

        first = executor.health()
        second = executor.health()

        self.assertTrue(first["ready"])
        self.assertTrue(first["connection_tested"])
        self.assertTrue(first["connection_ready"])
        self.assertEqual(first, second)
        connect.assert_called_once()
        cursor.execute.assert_called_once_with("SELECT 1")

    @patch("pymysql.connect")
    def test_health_reports_connection_failure_without_private_message(
        self,
        connect: MagicMock,
    ) -> None:
        connect.side_effect = OSError(10060, "connection timed out for private-host")
        executor = DorisExecutor(
            host="127.0.0.1",
            port=9030,
            user="readonly",
            password="unused",
            database="ywzt_ods",
            timeout_seconds=60,
            max_rows=10,
        )

        result = executor.health()

        self.assertFalse(result["ready"])
        self.assertTrue(result["connection_tested"])
        self.assertFalse(result["connection_ready"])
        self.assertEqual("timeout", result["connection_error_type"])
        self.assertEqual(10060, result["connection_error_code"])
        self.assertNotIn("private-host", str(result))

    @patch("pymysql.connect")
    def test_empty_bindings_do_not_percent_format_sql(self, connect: MagicMock) -> None:
        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = None
        cursor.description = ()
        executor = DorisExecutor(
            host="127.0.0.1",
            port=9030,
            user="readonly",
            password="unused",
            database="ywzt_ods",
            timeout_seconds=60,
            max_rows=10,
        )
        sql = "SELECT DATE_FORMAT(NOW(), '%Y-%m') AS month_value"
        executor.execute(sql, {}, metric_id="adhoc.sql")
        cursor.execute.assert_called_once_with(sql)

    @patch("pymysql.connect")
    def test_streaming_cursor_caps_rows_and_deduplicates_columns(
        self,
        connect: MagicMock,
    ) -> None:
        import pymysql

        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.description = (("id",), ("id",))
        cursor.fetchone.side_effect = [(1, 2), (3, 4), (5, 6)]
        executor = DorisExecutor(
            host="127.0.0.1",
            port=9030,
            user="readonly",
            password="unused",
            database="ywzt_ods",
            timeout_seconds=60,
            max_rows=2,
        )

        result = executor.execute("SELECT 1", {}, metric_id="adhoc.sql")

        self.assertEqual(("id", "id__2"), result.columns)
        self.assertEqual(
            ({"id": 1, "id__2": 2}, {"id": 3, "id__2": 4}),
            result.rows,
        )
        self.assertTrue(result.truncated)
        self.assertEqual("row_limit", result.truncation_reason)
        self.assertEqual(3, cursor.fetchone.call_count)
        self.assertIs(
            pymysql.cursors.SSCursor,
            connect.call_args.kwargs["cursorclass"],
        )
        connection.close.assert_called_once()

    @patch("pymysql.connect")
    def test_streaming_cursor_caps_response_bytes(self, connect: MagicMock) -> None:
        connection = connect.return_value
        cursor = connection.cursor.return_value
        cursor.description = (("large_value",),)
        cursor.fetchone.return_value = ("x" * 100,)
        executor = DorisExecutor(
            host="127.0.0.1",
            port=9030,
            user="readonly",
            password="unused",
            database="ywzt_ods",
            timeout_seconds=60,
            max_rows=10,
            max_response_bytes=32,
        )

        result = executor.execute("SELECT 1", {}, metric_id="adhoc.sql")

        self.assertEqual((), result.rows)
        self.assertTrue(result.truncated)
        self.assertEqual("response_byte_limit", result.truncation_reason)
        self.assertEqual(1, cursor.fetchone.call_count)
        connection.close.assert_called_once()


class RuntimeSettingsPolicyTestCase(unittest.TestCase):
    def test_source_policy_hard_caps_cannot_be_expanded(self) -> None:
        cases = (
            {"max_rows": 100001},
            {"max_rows": 0},
            {"query_timeout_seconds": 181},
            {"query_timeout_seconds": 0},
            {"max_sql_length": 100001},
            {"max_response_bytes": 64 * 1024 * 1024 + 1},
            {"max_response_bytes": 1023},
            {"doris_port": 65536},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                RuntimeSettings(project_root=PROJECT_ROOT, **overrides)

        with self.assertRaises(ValueError):
            AdHocSqlValidator(allowed_schemas=("*",))


if __name__ == "__main__":
    unittest.main()
