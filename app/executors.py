from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
import json
from pathlib import Path
import socket
import threading
from time import monotonic, perf_counter, sleep
from typing import Any

from .errors import ExecutorUnavailableError, QueryExecutionError
from .models import ExecutionResult
from .serialization import to_json_value


DORIS_HEALTH_CACHE_SECONDS = 10.0
DORIS_HEALTH_CONNECT_TIMEOUT_SECONDS = 5
DORIS_CONNECT_RETRY_COUNT = 1
DORIS_CONNECT_RETRY_BACKOFF_SECONDS = 0.25
DORIS_QUERY_RETRY_COUNT = 1
DORIS_QUERY_RETRY_BACKOFF_SECONDS = 0.35
DORIS_FAILURE_BACKOFF_SECONDS = 5.0
_DORIS_TRANSIENT_ERROR_CODES = frozenset({2003, 2006, 2013, 2055})


def _connection_error_class(exc: Exception) -> str:
    """Reduce driver-specific failures to stable, non-sensitive health labels."""

    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if isinstance(exc, (TimeoutError, socket.timeout)) or code in {110, 10060}:
        return "timeout"
    if code in {111, 10061}:
        return "refused"
    if code in {1045, 28000}:
        return "authentication"
    return "connection_error"


def _is_transient_transport_error(exc: Exception) -> bool:
    """Identify a lost connection without retrying SQL or auth failures."""

    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if code in _DORIS_TRANSIENT_ERROR_CODES:
        return True
    if code in {1045, 28000}:
        return False
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, BrokenPipeError, EOFError, OSError)):
        return True
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "server has gone away",
            "lost connection",
            "connection reset",
            "connection closed",
            "broken pipe",
            "end of stream",
            "timed out",
            "timeout",
            "eof",
        )
    )


def _unique_column_names(description: Any) -> tuple[str, ...]:
    names: list[str] = []
    used: set[str] = set()
    for item in description or ():
        base = str(item[0])
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}__{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        names.append(candidate)
    return tuple(names)


class QueryExecutor(ABC):
    name = "base"

    @abstractmethod
    def execute(self, sql: str, bindings: dict[str, Any], *, metric_id: str) -> ExecutionResult:
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": True}


class MockExecutor(QueryExecutor):
    name = "mock"

    def __init__(self, fixture_path: Path | None = None, *, max_rows: int = 100000) -> None:
        self.fixture_path = fixture_path
        self.max_rows = max_rows
        self._fixtures = self._load_fixtures(fixture_path)

    @staticmethod
    def _load_fixtures(path: Path | None) -> dict[str, list[dict[str, Any]]]:
        if path is None:
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutorUnavailableError(
                f"Unable to load mock fixture file: {path}",
                details={"error": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise ExecutorUnavailableError("Mock fixture file must contain a JSON object")
        fixtures: dict[str, list[dict[str, Any]]] = {}
        for metric_id, rows in value.items():
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ExecutorUnavailableError(f"Mock fixture for {metric_id} must be a list of objects")
            fixtures[str(metric_id)] = rows
        return fixtures

    def execute(self, sql: str, bindings: dict[str, Any], *, metric_id: str) -> ExecutionResult:
        started = perf_counter()
        fixture_rows = self._fixtures.get(metric_id, [])
        truncated = len(fixture_rows) > self.max_rows
        selected = fixture_rows[: self.max_rows]
        columns: tuple[str, ...] = tuple(selected[0].keys()) if selected else ()
        rows = tuple(to_json_value(row) for row in selected)
        return ExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=max(0, round((perf_counter() - started) * 1000)),
            executor_name=self.name,
            is_mock=True,
            truncation_reason="row_limit" if truncated else None,
        )

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ready": True,
            "fixture_path": str(self.fixture_path) if self.fixture_path else None,
            "fixture_metric_count": len(self._fixtures),
            "warning": "Mock mode never queries Doris.",
        }


class DorisExecutor(QueryExecutor):
    name = "doris"

    def __init__(
        self,
        *,
        host: str | None,
        port: int,
        user: str | None,
        password: str | None,
        database: str | None,
        timeout_seconds: int,
        max_rows: int,
        max_response_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self.max_response_bytes = max_response_bytes
        self._health_lock = threading.Lock()
        self._health_checked_at = 0.0
        self._health_result: tuple[bool, str | None, int | None] | None = None

    def _configuration_errors(self) -> list[str]:
        missing = []
        if not self.host:
            missing.append("DORIS_HOST")
        if not self.user:
            missing.append("DORIS_USER")
        if self.password is None:
            missing.append("DORIS_PASSWORD")
        return missing

    def _probe_connection(self) -> tuple[bool, str | None, int | None]:
        """Check the FE handshake and a trivial read without retaining a connection."""

        missing = self._configuration_errors()
        if missing:
            return False, None, None
        try:
            import pymysql
        except ImportError:
            return False, "ImportError", None

        connection = None
        cursor = None
        try:
            connection = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset="utf8mb4",
                connect_timeout=min(self.timeout_seconds, DORIS_HEALTH_CONNECT_TIMEOUT_SECONDS),
                read_timeout=DORIS_HEALTH_CONNECT_TIMEOUT_SECONDS,
                write_timeout=DORIS_HEALTH_CONNECT_TIMEOUT_SECONDS,
                autocommit=True,
                cursorclass=pymysql.cursors.Cursor,
            )
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            return True, None, None
        except Exception as exc:  # pragma: no cover - depends on the live FE
            db_error_code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            return False, _connection_error_class(exc), db_error_code
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _cached_connection_health(self) -> tuple[bool, str | None, int | None]:
        now = monotonic()
        with self._health_lock:
            if (
                self._health_result is not None
                and now - self._health_checked_at < DORIS_HEALTH_CACHE_SECONDS
            ):
                return self._health_result
            result = self._probe_connection()
            self._health_checked_at = monotonic()
            self._health_result = result
            return result

    def _connect(self, pymysql: Any) -> Any:
        """Open the FE connection, retrying only transient connect failures."""

        kwargs = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": "utf8mb4",
            "connect_timeout": min(self.timeout_seconds, 10),
            "read_timeout": self.timeout_seconds,
            "write_timeout": self.timeout_seconds,
            "autocommit": True,
            "cursorclass": pymysql.cursors.SSCursor,
        }
        for attempt in range(DORIS_CONNECT_RETRY_COUNT + 1):
            try:
                connection = pymysql.connect(**kwargs)
                with self._health_lock:
                    self._health_checked_at = monotonic()
                    self._health_result = (True, None, None)
                return connection
            except Exception as exc:
                error_code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
                retryable = (
                    isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, OSError))
                    or error_code in {2003, 2006, 2013, 2055}
                )
                if attempt >= DORIS_CONNECT_RETRY_COUNT or not retryable:
                    with self._health_lock:
                        self._health_checked_at = monotonic()
                        self._health_result = (
                            False,
                            _connection_error_class(exc),
                            error_code,
                        )
                    raise
                sleep(DORIS_CONNECT_RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise RuntimeError("Doris connection retry loop terminated unexpectedly")

    def health(self) -> dict[str, Any]:
        missing = self._configuration_errors()
        if not missing:
            try:
                import pymysql  # noqa: F401
            except ImportError:
                missing.append("python package PyMySQL")

        connection_tested = False
        connection_ready = False
        connection_error_type = None
        connection_error_code = None
        if not missing:
            connection_ready, connection_error_type, connection_error_code = (
                self._cached_connection_health()
            )
            connection_tested = True
        return {
            "name": self.name,
            "ready": not missing and connection_ready,
            "missing": missing,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "connection_tested": connection_tested,
            "connection_ready": connection_ready,
            "connection_error_type": connection_error_type,
            "connection_error_code": connection_error_code,
        }

    def execute(self, sql: str, bindings: dict[str, Any], *, metric_id: str) -> ExecutionResult:
        """Execute once, replaying only transport failures on a fresh connection."""

        for attempt in range(DORIS_QUERY_RETRY_COUNT + 1):
            try:
                return self._execute_once(sql, bindings, metric_id=metric_id)
            except QueryExecutionError as exc:
                details = exc.details if isinstance(exc.details, Mapping) else {}
                error_code = details.get("db_error_code")
                retryable = (
                    error_code in _DORIS_TRANSIENT_ERROR_CODES
                    or _is_transient_transport_error(
                        RuntimeError(
                            f"{details.get('error_type', '')} {details.get('message', '')}"
                        )
                    )
                )
                if attempt >= DORIS_QUERY_RETRY_COUNT or not retryable:
                    raise
                sleep(DORIS_QUERY_RETRY_BACKOFF_SECONDS * (attempt + 1))

        raise RuntimeError("Doris query retry loop terminated unexpectedly")

    def _execute_once(
        self,
        sql: str,
        bindings: dict[str, Any],
        *,
        metric_id: str,
    ) -> ExecutionResult:
        missing = self._configuration_errors()
        if missing:
            raise ExecutorUnavailableError(
                "Doris executor is explicitly selected but is not configured",
                details={"missing": missing},
            )
        try:
            import pymysql
        except ImportError as exc:
            raise ExecutorUnavailableError(
                "PyMySQL is required for Doris execution",
                details={"install": "python -m pip install PyMySQL"},
            ) from exc

        with self._health_lock:
            cached_health = self._health_result
            health_age = monotonic() - self._health_checked_at
        if (
            cached_health is not None
            and not cached_health[0]
            and health_age < DORIS_FAILURE_BACKOFF_SECONDS
        ):
            raise ExecutorUnavailableError(
                "Doris connection is temporarily unavailable",
                details={
                    "reason": "connection_backoff",
                    "error_type": cached_health[1],
                    "db_error_code": cached_health[2],
                },
            )

        started = perf_counter()
        connection = None
        cursor = None
        truncated = False
        truncation_reason = None
        selected: list[dict[str, Any]] = []
        columns: tuple[str, ...] = ()
        phase = "connect"
        try:
            connection = self._connect(pymysql)
            cursor = connection.cursor()
            phase = "query"
            if bindings:
                cursor.execute(sql, bindings)
            else:
                cursor.execute(sql)
            columns = _unique_column_names(cursor.description)
            response_bytes = 2
            exhausted = False
            while len(selected) < self.max_rows:
                raw_row = cursor.fetchone()
                if raw_row is None:
                    exhausted = True
                    break
                if isinstance(raw_row, Mapping):
                    row = {
                        column: to_json_value(raw_row.get(column))
                        for column in columns
                    }
                else:
                    row = {
                        column: to_json_value(value)
                        for column, value in zip(columns, raw_row, strict=False)
                    }
                row_bytes = len(
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ) + 1
                if response_bytes + row_bytes > self.max_response_bytes:
                    truncated = True
                    truncation_reason = "response_byte_limit"
                    break
                selected.append(row)
                response_bytes += row_bytes
            if not truncated and not exhausted and len(selected) == self.max_rows:
                if cursor.fetchone() is not None:
                    truncated = True
                    truncation_reason = "row_limit"
            if truncated:
                # Closing the socket avoids draining an unbuffered result after either cap.
                cursor.connection = None
                connection.close()
                connection = None
        except Exception as exc:
            error_code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            raise QueryExecutionError(
                "Doris query failed",
                details={
                    "error_type": type(exc).__name__,
                    "failure_class": (
                        _connection_error_class(exc)
                        if phase == "connect" or _is_transient_transport_error(exc)
                        else "query_error"
                    ),
                    "db_error_code": error_code,
                    "message": str(exc),
                },
            ) from exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    # Preserve the query failure when an unbuffered cursor also fails to close.
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        rows = tuple(selected)
        return ExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=max(0, round((perf_counter() - started) * 1000)),
            executor_name=self.name,
            is_mock=False,
            truncation_reason=truncation_reason,
        )


def build_executor(settings: Any) -> QueryExecutor:
    mode = settings.executor_mode
    if mode == "mock":
        return MockExecutor(settings.mock_fixture_path, max_rows=settings.max_rows)
    if mode == "doris":
        return DorisExecutor(
            host=settings.doris_host,
            port=settings.doris_port,
            user=settings.doris_user,
            password=settings.doris_password,
            database=settings.doris_database,
            timeout_seconds=settings.query_timeout_seconds,
            max_rows=settings.max_rows,
            max_response_bytes=settings.max_response_bytes,
        )
    raise ExecutorUnavailableError(
        "DORIS_EXECUTION_MODE must be mock or doris",
        details={"value": mode},
    )
