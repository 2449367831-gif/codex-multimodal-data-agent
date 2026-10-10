from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any
from uuid import uuid4

from .audit import AuditStore
from .errors import (
    ExecutorUnavailableError,
    ParameterValidationError,
    RuntimeErrorBase,
    SqlSafetyError,
)
from .executors import QueryExecutor, build_executor
from .models import utc_now_iso
from .settings import RuntimeSettings
from .sql import strip_sql_comments


ADHOC_METRIC_ID = "adhoc.sql"
LOGGER = logging.getLogger(__name__)
DANGEROUS_QUERY_FUNCTIONS = {
    "ai_agg",
    "ai_classify",
    "ai_filter",
    "ai_generate",
    "ai_mask",
    "ai_sentiment",
    "ai_similarity",
    "ai_translate",
    "benchmark",
    "get_lock",
    "http_get",
    "http_post",
    "load_file",
    "is_free_lock",
    "is_used_lock",
    "release_all_locks",
    "release_lock",
    "sleep",
    "sleep_ms",
}


def _normalized_identifier(value: str) -> str:
    return value.replace("`", "").strip().casefold()


@dataclass(frozen=True)
class ValidatedAdHocSql:
    sql: str
    source_tables: tuple[str, ...]
    validation_results: tuple[dict[str, Any], ...]


class AdHocSqlValidator:
    """Fail-closed validation for model-generated Doris queries."""

    def __init__(
        self,
        *,
        allowed_schemas: tuple[str, ...],
        allowed_catalogs: tuple[str, ...] = ("internal",),
        max_rows: int = 100000,
        max_sql_length: int = 100000,
    ) -> None:
        if not 1 <= max_rows <= 100000:
            raise ValueError("max_rows must be between 1 and the 100000-row hard cap")
        if max_sql_length < 1:
            raise ValueError("max_sql_length must be positive")
        schemas = tuple(sorted({_normalized_identifier(item) for item in allowed_schemas if item}))
        catalogs = tuple(sorted({_normalized_identifier(item) for item in allowed_catalogs if item}))
        if not schemas:
            raise ValueError("At least one SQL schema must be allowed")
        if "*" in schemas or "*" in catalogs:
            raise ValueError(
                "Wildcard SQL schema/catalog allowlists are not permitted; list each scope explicitly"
            )
        self.allowed_schemas = schemas
        self.allowed_catalogs = catalogs
        self.max_rows = max_rows
        self.max_sql_length = max_sql_length

    def validate(self, sql: str) -> ValidatedAdHocSql:
        if not isinstance(sql, str) or not sql.strip():
            raise ParameterValidationError("sql must be a non-empty string")
        if len(sql) > self.max_sql_length:
            raise SqlSafetyError(
                "SQL exceeds the configured length limit",
                details={"max_sql_length": self.max_sql_length},
            )

        clean = strip_sql_comments(sql).strip()
        if not clean:
            raise SqlSafetyError("SQL contains no executable statement")

        try:
            import sqlglot
            from sqlglot import expressions as exp
            from sqlglot.optimizer.scope import traverse_scope
        except ImportError as exc:  # pragma: no cover - required runtime dependency
            raise ExecutorUnavailableError(
                "SQLGlot is required for model-generated SQL execution"
            ) from exc

        try:
            statements = [
                statement
                for statement in sqlglot.parse(clean, read="doris")
                if statement is not None
            ]
        except Exception as exc:
            raise SqlSafetyError(
                "SQLGlot could not parse the Doris query",
                details={"error": str(exc)},
            ) from exc
        if len(statements) != 1:
            raise SqlSafetyError("Exactly one SQL statement is required")

        root = statements[0]
        if not isinstance(root, exp.Query) or root.find(exp.Select) is None:
            raise SqlSafetyError("Only SELECT or WITH ... SELECT is permitted")

        forbidden_types = tuple(
            node_type
            for name in (
                "Alter",
                "Analyze",
                "Command",
                "Commit",
                "Copy",
                "Create",
                "Delete",
                "Drop",
                "Execute",
                "Export",
                "Grant",
                "Insert",
                "LoadData",
                "Merge",
                "Pragma",
                "Revoke",
                "Rollback",
                "Set",
                "Transaction",
                "TruncateTable",
                "Update",
                "Use",
            )
            if (node_type := getattr(exp, name, None)) is not None
        )
        if any(isinstance(root, node_type) or root.find(node_type) for node_type in forbidden_types):
            raise SqlSafetyError("Only a read-only SELECT statement is permitted")
        if root.find(exp.Into) is not None or root.find(exp.Lock) is not None:
            raise SqlSafetyError("SELECT INTO, OUTFILE, and locking clauses are not permitted")
        if any(
            bool(with_clause.args.get("recursive"))
            for with_clause in root.find_all(exp.With)
        ):
            raise SqlSafetyError("Recursive CTEs are not permitted")
        forbidden_variable_types = tuple(
            node_type
            for name in ("Parameter", "PropertyEQ", "SessionParameter")
            if (node_type := getattr(exp, name, None)) is not None
        )
        if any(root.find(node_type) for node_type in forbidden_variable_types):
            raise SqlSafetyError(
                "Session variables, global variables, and variable assignment are not permitted"
            )

        dangerous_functions = sorted(
            {
                str(function.name or "").casefold()
                for function in root.find_all(exp.Anonymous)
                if (
                    str(function.name or "").casefold() in DANGEROUS_QUERY_FUNCTIONS
                    or str(function.name or "").casefold().startswith(("ai_", "sleep"))
                    or "_lock" in str(function.name or "").casefold()
                )
            }
        )
        if dangerous_functions:
            raise SqlSafetyError(
                "Dangerous SQL functions are not permitted",
                details={"functions": dangerous_functions},
            )

        source_tables: list[str] = []
        unauthorized_schemas: set[str] = set()
        unauthorized_catalogs: set[str] = set()
        physical_tables = (
            source
            for scope in traverse_scope(root)
            for source in scope.sources.values()
            if isinstance(source, exp.Table)
        )
        for table in physical_tables:
            if not isinstance(table.this, exp.Identifier):
                raise SqlSafetyError(
                    "Table-valued functions and external table functions are not permitted"
                )
            name = _normalized_identifier(str(table.name or ""))
            schema = _normalized_identifier(str(table.db or ""))
            catalog = _normalized_identifier(str(table.catalog or ""))
            if not schema:
                raise SqlSafetyError(
                    "Physical tables must be fully qualified with a schema",
                    details={"table": name},
                )
            if schema not in self.allowed_schemas:
                unauthorized_schemas.add(schema)
            if (
                catalog
                and catalog not in self.allowed_catalogs
            ):
                unauthorized_catalogs.add(catalog)
            source_tables.append(".".join(part for part in (catalog, schema, name) if part))

        if unauthorized_schemas:
            raise SqlSafetyError(
                "SQL references schemas outside the backend allowlist",
                details={
                    "unauthorized_schemas": sorted(unauthorized_schemas),
                    "allowed_schemas": list(self.allowed_schemas),
                },
            )
        if unauthorized_catalogs:
            raise SqlSafetyError(
                "SQL references catalogs outside the backend allowlist",
                details={
                    "unauthorized_catalogs": sorted(unauthorized_catalogs),
                    "allowed_catalogs": list(self.allowed_catalogs),
                },
            )

        limit = root.args.get("limit")
        limit_cap = self.max_rows + 1
        if limit is None:
            execution_root = root.limit(limit_cap)
        else:
            limit_expression = limit.expression
            if not isinstance(limit_expression, exp.Literal) or not limit_expression.is_int:
                raise SqlSafetyError(
                    "LIMIT must be a non-negative integer literal",
                    details={"max_limit": limit_cap},
                )
            limit_value = int(limit_expression.this)
            if limit_value < 0 or limit_value > limit_cap:
                raise SqlSafetyError(
                    "LIMIT exceeds the configured result cap",
                    details={"requested_limit": limit_value, "max_limit": limit_cap},
                )
            execution_root = root

        statement_sql = execution_root.sql(dialect="doris")

        relations = tuple(sorted(set(source_tables)))
        validations = (
            {
                "id": "single_statement_read_only_ast",
                "passed": True,
                "engine": "sqlglot_doris",
            },
            {
                "id": "schema_and_catalog_allowlist",
                "passed": True,
                "allowed_schemas": list(self.allowed_schemas),
                "allowed_catalogs": list(self.allowed_catalogs),
                "source_tables": list(relations),
            },
            {
                "id": "result_row_cap",
                "passed": True,
                "max_rows": self.max_rows,
                "server_limit": limit_cap,
            },
        )
        return ValidatedAdHocSql(
            sql=statement_sql,
            source_tables=relations,
            validation_results=validations,
        )


class AdHocSqlService:
    """Execute validated SQL without loading or bypassing metric release state."""

    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        executor: QueryExecutor | None = None,
        audit_store: AuditStore | None = None,
        validator: AdHocSqlValidator | None = None,
    ) -> None:
        self.settings = settings or RuntimeSettings.from_env()
        self.executor = executor or build_executor(self.settings)
        self.audit = audit_store or AuditStore(self.settings.audit_db_path)
        self.validator = validator or AdHocSqlValidator(
            allowed_schemas=self.settings.sql_allowed_schemas,
            allowed_catalogs=self.settings.sql_allowed_catalogs,
            max_rows=self.settings.max_rows,
            max_sql_length=self.settings.max_sql_length,
        )

    def close(self) -> None:
        self.audit.close()

    def health(self) -> dict[str, Any]:
        executor_health = self.executor.health()
        ready = self.executor.name == "doris" and bool(executor_health.get("ready"))
        public_executor_health = {
            key: executor_health.get(key)
            for key in (
                "name",
                "ready",
                "missing",
                "database",
                "connection_tested",
                "connection_ready",
                "connection_error_type",
                "connection_error_code",
            )
            if key in executor_health
        }
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "executor": public_executor_health,
            "sql_policy": {
                "statements": ["SELECT", "WITH ... SELECT"],
                "allowed_schemas": list(self.validator.allowed_schemas),
                "allowed_catalogs": list(self.validator.allowed_catalogs),
                "max_rows": self.validator.max_rows,
                "max_sql_length": self.validator.max_sql_length,
                "max_response_bytes": self.settings.max_response_bytes,
                "fully_qualified_tables_required": True,
                "external_table_functions_allowed": False,
                "row_expansion_functions_allowed": True,
                "per_user_authorization": "not_configured",
                "readiness_scope": "connection_probe",
                "readonly_privileges_verified": False,
                "database_resource_limits_verified": False,
            },
            "audit": {
                "enabled": self.settings.audit_db_path is not None,
                "stores_result_rows": False,
            },
        }

    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, Any]:
        if purpose is not None and (not isinstance(purpose, str) or len(purpose) > 1000):
            raise ParameterValidationError("purpose must be a string of at most 1000 characters")
        request_id = str(uuid4())
        now = utc_now_iso()
        validated: ValidatedAdHocSql | None = None
        try:
            if self.executor.name != "doris":
                raise ExecutorUnavailableError(
                    "Model-generated SQL requires DORIS_EXECUTION_MODE=doris"
                )
            validated = self.validator.validate(sql)
            self.audit.upsert(
                {
                    "request_id": request_id,
                    "created_at": now,
                    "updated_at": now,
                    "status": "running",
                    "metric_id": ADHOC_METRIC_ID,
                    "question": purpose,
                    "parameters": {},
                    "sql": validated.sql,
                    "bindings": {},
                    "validation_results": list(validated.validation_results),
                    "executor_name": self.executor.name,
                }
            )
            result = self.executor.execute(
                validated.sql,
                {},
                metric_id=ADHOC_METRIC_ID,
            )
        except RuntimeErrorBase as exc:
            if isinstance(exc, ExecutorUnavailableError) or exc.code == "query_execution_error":
                # Keep diagnostics useful without writing SQL, result rows, credentials,
                # hostnames, or backend error messages to the service log.
                LOGGER.warning(
                    "Doris request failed request_id=%s code=%s error_type=%s db_error_code=%s",
                    request_id,
                    exc.code,
                    exc.details.get("error_type"),
                    exc.details.get("db_error_code"),
                )
            self.audit.upsert(
                {
                    "request_id": request_id,
                    "created_at": now,
                    "updated_at": utc_now_iso(),
                    "status": "blocked" if isinstance(exc, SqlSafetyError) else "failed",
                    "metric_id": ADHOC_METRIC_ID,
                    "question": purpose,
                    "parameters": {},
                    "sql": (
                        validated.sql
                        if validated
                        else sql[: self.settings.max_sql_length]
                        if isinstance(sql, str)
                        else None
                    ),
                    "bindings": {},
                    "validation_results": (
                        list(validated.validation_results) if validated else []
                    ),
                    "executor_name": self.executor.name,
                    "error": exc.as_dict(),
                }
            )
            raise

        status = "succeeded"
        self.audit.upsert(
            {
                "request_id": request_id,
                "created_at": now,
                "updated_at": utc_now_iso(),
                "status": status,
                "metric_id": ADHOC_METRIC_ID,
                "question": purpose,
                "parameters": {},
                "sql": validated.sql,
                "bindings": {},
                "validation_results": list(validated.validation_results),
                "executor_name": self.executor.name,
                "duration_ms": result.duration_ms,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "is_mock": result.is_mock,
            }
        )
        return {
            "request_id": request_id,
            "status": status,
            "source_tables": list(validated.source_tables),
            "validation_results": list(validated.validation_results),
            "execution": result.as_dict(),
        }
