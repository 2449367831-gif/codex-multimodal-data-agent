from __future__ import annotations

from typing import Any

from .query_diagnostics import classify_query_error


class RuntimeErrorBase(Exception):
    """Base error with a stable API-facing code."""

    code = "runtime_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def as_public_dict(self) -> dict[str, Any]:
        return self.as_dict()


class KnowledgeBaseError(RuntimeErrorBase):
    code = "knowledge_base_error"


class MetricNotFoundError(RuntimeErrorBase):
    code = "metric_not_found"
    http_status = 404


class RouteAmbiguousError(RuntimeErrorBase):
    code = "metric_route_ambiguous"
    http_status = 409


class ParameterValidationError(RuntimeErrorBase):
    code = "invalid_parameters"
    http_status = 422


class TemplateNotExecutableError(RuntimeErrorBase):
    code = "template_not_executable"
    http_status = 409


class ContractNotApprovedError(RuntimeErrorBase):
    code = "contract_not_approved"
    http_status = 409


class UnapprovedContractResultError(RuntimeErrorBase):
    code = "unapproved_contract_result"
    http_status = 409


class UnsupportedTemplateParameterError(RuntimeErrorBase):
    code = "unsupported_template_parameter"
    http_status = 409


class SqlSafetyError(RuntimeErrorBase):
    code = "sql_safety_error"
    http_status = 409


class ExecutorUnavailableError(RuntimeErrorBase):
    code = "executor_unavailable"
    http_status = 503


class QueryCancelledError(RuntimeErrorBase):
    code = "query_cancelled"
    http_status = 499


class QueryExecutionError(RuntimeErrorBase):
    code = "query_execution_error"
    http_status = 502

    def as_public_dict(self) -> dict[str, Any]:
        public_details = {
            key: self.details[key]
            for key in ("error_type", "failure_class", "db_error_code")
            if key in self.details
        }
        diagnostic = classify_query_error(self.details)
        if diagnostic:
            public_details["diagnostic"] = diagnostic
        return {
            "code": self.code,
            "message": "Doris query execution failed",
            "details": public_details,
        }
