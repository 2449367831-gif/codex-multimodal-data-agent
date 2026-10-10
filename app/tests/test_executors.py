from __future__ import annotations

from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from app.errors import QueryExecutionError
from app.executors import DorisExecutor


class FakeDatabaseError(Exception):
    pass


class FakeCursor:
    description = (("value",),)

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.rows = [("ok",)]
        self.closed = False

    def execute(self, _sql: str, _bindings: object = None) -> None:
        if self.failures:
            raise self.failures.pop(0)

    def fetchone(self) -> object:
        return self.rows.pop(0) if self.rows else None

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_obj = cursor
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def close(self) -> None:
        self.closed = True


def fake_pymysql(connect):
    module = ModuleType("pymysql")
    module.connect = connect  # type: ignore[attr-defined]
    module.cursors = SimpleNamespace(SSCursor=object, Cursor=object)  # type: ignore[attr-defined]
    return module


class DorisExecutorRetryTests(unittest.TestCase):
    def executor(self) -> DorisExecutor:
        return DorisExecutor(
            host="doris.example",
            port=9030,
            user="readonly",
            password="secret",
            database="ywzt_ods",
            timeout_seconds=5,
            max_rows=10,
        )

    def test_retries_one_transient_query_connection_failure(self) -> None:
        connect_count = 0

        def connect(**_kwargs: object) -> FakeConnection:
            nonlocal connect_count
            connect_count += 1
            failure = FakeDatabaseError(2013, "Lost connection to Doris") if connect_count == 1 else None
            return FakeConnection(FakeCursor([failure] if failure else []))

        with patch.dict("sys.modules", {"pymysql": fake_pymysql(connect)}):
            result = self.executor().execute("SELECT 1", {}, metric_id="demo")

        self.assertEqual(("ok",), tuple(result.rows[0].values()))
        self.assertEqual(2, connect_count)

    def test_does_not_retry_a_business_sql_error(self) -> None:
        connect_count = 0

        def connect(**_kwargs: object) -> FakeConnection:
            nonlocal connect_count
            connect_count += 1
            return FakeConnection(FakeCursor([FakeDatabaseError(1064, "syntax error")]))

        with patch.dict("sys.modules", {"pymysql": fake_pymysql(connect)}):
            with self.assertRaises(QueryExecutionError):
                self.executor().execute("SELECT broken", {}, metric_id="demo")

        self.assertEqual(1, connect_count)


if __name__ == "__main__":
    unittest.main()
