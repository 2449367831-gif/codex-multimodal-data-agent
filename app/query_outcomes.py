"""Shared, data-free status contract for Doris HTTP, MCP and chat boundaries."""

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import RuntimeErrorBase
from .query_diagnostics import classify_query_error, safe_diagnostic


_ERRORS = {
    "plan_invalid": ("query_plan_invalid", "failed", 502, "查询计划未能解析，尚未执行取数；请重试本次问题"),
    "backend_rejected": ("query_rejected", "rejected", 409, "查询被后端限制拒绝，未执行取数"),
    "query_failed": ("query_failed", "failed", 502, "查询执行失败，未返回可确认结果"),
    "backend_unavailable": ("query_unavailable", "failed", 503, "取数服务连接失败，请稍后重试"),
    "query_timeout": ("query_timeout", "failed", 504, "查询执行超时，未返回完整结果"),
    "result_truncated": ("query_truncated", "truncated", 409, "查询结果已截断，不能作为完整结果；请分段查询并对账"),
}
_INVALID = ("query_invalid_result", "failed", 502, "查询结果校验未通过，未展示数据")
_INVALID_REASONS = {"execution_missing", "execution_invalid", "not_live_result", "rows_invalid", "row_count_mismatch", "reconciliation_failed"}
_INVALID_MESSAGES = {
    "reconciliation_failed": "查询已执行，但对账校验未通过，未展示结果",
    "execution_invalid": "查询结果校验未通过：返回结果结构与查询计划不一致，未展示数据",
    "not_live_result": "查询已返回，但不是可确认的实时生产数据，未展示数据",
    "rows_invalid": "Doris 查询已完成，但返回行结构无法解析，未展示数据",
    "row_count_mismatch": "Doris 查询已完成，但返回行数与数据不一致，未展示数据",
}

NORMALIZED_RESULT_STATES = frozenset({
    "succeeded", "empty_result", "query_rejected", "query_failed",
    "query_unavailable", "truncated_result", "invalid_result",
})

VALIDATION_FAILURES = {
    "inventory_net_nonnegative": (
        "inventory_quality_failed",
        "库存净额校验未通过，本次未发布库存或周转结果。请核对库存来源中的负数与调整记录。",
    ),
    "source_freshness": (
        "source_freshness_failed",
        "数据时效校验未通过，本次未发布结果。请确认数据同步时间和查询区间是否完整。",
    ),
    "control_total_reconciliation": (
        "control_reconciliation_failed",
        "汇总与独立对账结果不一致，本次未发布数据。请核对来源数据和统计范围。",
    ),
    "component_scope_equivalence": (
        "component_scope_failed",
        "指标各组成部分的统计范围不一致，本次未计算合计或比率。",
    ),
}


class DorisResultError(RuntimeErrorBase, RuntimeError):
    def __init__(self, reason: str, *, diagnostic: str | None = None,
                 validation_rule: str | None = None,
                 missing_fields: tuple[str, ...] = (),
                 repairable: bool = False):
        # A structured backend query failure may be recoverable by one bounded
        # model re-plan. Keep that host-only signal separate from the public
        # error vocabulary so it never becomes user-visible or an instruction
        # to retry rejected, timed out, or invalid-result requests.
        self.repairable = repairable is True
        self.diagnostic = safe_diagnostic(diagnostic)
        self.missing_fields = tuple(
            field for field in missing_fields
            if isinstance(field, str) and field and len(field) <= 80
        )
        self.reason = reason if reason in _ERRORS or reason in _INVALID_REASONS else "query_failed"
        self.code, self.result_state, self.http_status, message = _ERRORS.get(self.reason, _INVALID)
        message = _INVALID_MESSAGES.get(self.reason, message)
        self.normalized_state = _normalized_state_for_reason(self.reason)
        if self.reason == "reconciliation_failed" and isinstance(validation_rule, str) and validation_rule in VALIDATION_FAILURES:
            self.code, message = VALIDATION_FAILURES[validation_rule]
        super().__init__(message, details={})

    def as_public_dict(self):
        details = {"missing_fields": list(self.missing_fields)} if self.missing_fields else {}
        message = self.message
        if self.normalized_state == "invalid_result" and self.missing_fields:
            message = (
                "Doris 查询已完成，但 Agent 未能解析返回结果，缺少字段："
                + "、".join(self.missing_fields)
            )
        return {
            "code": self.code,
            "message": message,
            "result_state": self.result_state,
            "normalized_state": self.normalized_state,
            "normalized_result_state": self.normalized_state,
            **({"failure_reason": self.reason} if self.reason in _INVALID_REASONS else {}),
            "details": details,
        }

    def as_tool_dict(self):
        payload = self.as_public_dict()
        if self.diagnostic:
            payload["diagnostic"] = self.diagnostic
        return payload


def _field(value: Any, *names: str) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _normalized_state_for_reason(reason: str) -> str:
    return {
        "plan_invalid": "invalid_result",
        "backend_rejected": "query_rejected",
        "query_rejected": "query_rejected",
        "backend_unavailable": "query_unavailable",
        "query_unavailable": "query_unavailable",
        "query_timeout": "query_failed",
        "result_truncated": "truncated_result",
        "execution_missing": "invalid_result",
        "execution_invalid": "invalid_result",
        "not_live_result": "invalid_result",
        "rows_invalid": "invalid_result",
        "row_count_mismatch": "invalid_result",
        "invalid_result": "invalid_result",
    }.get(reason, "query_failed")


def _error_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _error_state(error: Any, result_state: Any = None) -> tuple[str, str]:
    value = _error_mapping(error)
    code = str(value.get("code") or "").casefold()
    state = str(result_state or value.get("result_state") or "").casefold()
    if code in {"query_rejected", "backend_rejected", "sql_safety_error", "forbidden", "contract_not_approved"} or state in {"rejected", "query_rejected"}:
        return "query_rejected", "backend_rejected"
    if code in {"query_unavailable", "backend_unavailable", "executor_unavailable", "service_unavailable"} or state in {"unavailable", "query_unavailable"}:
        return "query_unavailable", "backend_unavailable"
    if code in {"query_truncated", "result_truncated", "truncated_result"} or state in {"truncated", "truncated_result"}:
        return "truncated_result", "result_truncated"
    if code in {"invalid_result", "query_invalid_result", "execution_invalid", "execution_missing"} or state in {"invalid", "invalid_result"}:
        return "invalid_result", "execution_invalid"
    return "query_failed", "query_failed"


@dataclass(frozen=True)
class NormalizedDorisToolResult:
    """Safe, transport-independent result classification for one Doris call."""

    state: str
    payload: Mapping[str, Any] | None = None
    execution: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    reason: str | None = None
    missing_fields: tuple[str, ...] = ()


def normalize_doris_tool_result(result: Any) -> NormalizedDorisToolResult:
    """Prefer structuredContent and never infer business data from text content."""

    structured = _field(result, "structuredContent", "structured_content")
    if isinstance(structured, Mapping):
        payload: Mapping[str, Any] = dict(structured)
    elif isinstance(result, Mapping) and ("status" in result or "execution" in result):
        # The local HTTP backend already returns the structured payload itself.
        payload = dict(result)
    else:
        raw_error = _field(result, "error")
        error = dict(raw_error) if isinstance(raw_error, Mapping) else None
        return NormalizedDorisToolResult(
            "invalid_result", reason="execution_missing",
            error=error,
            missing_fields=("structuredContent", "structuredContent.execution"),
        )

    status = str(payload.get("status") or "").casefold()
    if status != "succeeded":
        state, reason = _error_state(payload.get("error"), payload.get("result_state"))
        error = _error_mapping(payload.get("error"))
        return NormalizedDorisToolResult(state, payload=payload, error=error, reason=reason)

    execution = payload.get("execution")
    if not isinstance(execution, Mapping):
        return NormalizedDorisToolResult(
            "invalid_result", payload=payload, reason="execution_missing",
            missing_fields=("execution",),
        )
    missing: list[str] = []
    if not isinstance(execution.get("columns"), list):
        missing.append("execution.columns")
    if not isinstance(execution.get("rows"), list):
        missing.append("execution.rows")
    if type(execution.get("row_count")) is not int:
        missing.append("execution.row_count")
    if type(execution.get("truncated")) is not bool:
        missing.append("execution.truncated")
    if type(execution.get("is_mock")) is not bool:
        missing.append("execution.is_mock")
    if missing:
        return NormalizedDorisToolResult(
            "invalid_result", payload=payload, execution=execution,
            reason="execution_invalid", missing_fields=tuple(missing),
        )
    if execution["truncated"] is True:
        return NormalizedDorisToolResult(
            "truncated_result", payload=payload, execution=execution,
            reason="result_truncated",
        )
    if execution["is_mock"] is True:
        return NormalizedDorisToolResult(
            "invalid_result", payload=payload, execution=execution,
            reason="not_live_result",
        )
    rows = execution["rows"]
    if any(not isinstance(row, Mapping) for row in rows):
        return NormalizedDorisToolResult(
            "invalid_result", payload=payload, execution=execution,
            reason="rows_invalid",
        )
    if execution["row_count"] != len(rows):
        return NormalizedDorisToolResult(
            "invalid_result", payload=payload, execution=execution,
            reason="row_count_mismatch",
        )
    return NormalizedDorisToolResult(
        "empty_result" if not rows else "succeeded",
        payload=payload,
        execution=execution,
    )


# Short alias for callers that do not need the Doris-specific name.
normalize_tool_result = normalize_doris_tool_result


def backend_error(http_status: int, error: Any) -> DorisResultError:
    value = error if isinstance(error, Mapping) else {}
    code = value.get("code") if isinstance(value.get("code"), str) else ""
    details = value.get("details") if isinstance(value.get("details"), Mapping) else {}
    missing_fields = tuple(
        field for field in (details.get("missing_fields") or ())
        if isinstance(field, str) and field and len(field) <= 80
    ) if isinstance(details, Mapping) else ()
    # The HTTP boundary nests Doris diagnostics under ``details``.  Preserve
    # only the fixed vocabulary so the model can repair a query without
    # exposing database messages, identifiers, or SQL to the public client.
    diagnostic = (
        safe_diagnostic(value.get("diagnostic"))
        or safe_diagnostic(details.get("diagnostic"))
        or classify_query_error(value)
        or classify_query_error(details)
    )
    if diagnostic == "permission_denied":
        return DorisResultError("backend_rejected", diagnostic=diagnostic)
    for rule, (public_code, _) in VALIDATION_FAILURES.items():
        if code == public_code:
            return DorisResultError("reconciliation_failed", diagnostic="reconciliation_failed", validation_rule=rule)
    public_codes = {definition[0]: reason for reason, definition in _ERRORS.items()}
    if code in public_codes:
        return DorisResultError(public_codes[code], diagnostic=diagnostic)
    if code == "query_invalid_result":
        reason = value.get("failure_reason")
        if not isinstance(reason, str) or reason not in _INVALID_REASONS:
            reason = "execution_invalid"
        return DorisResultError(reason, diagnostic=diagnostic, missing_fields=missing_fields)
    if code in {"sql_safety_error", "invalid_parameters", "contract_not_approved", "forbidden"}:
        return DorisResultError("backend_rejected")
    if code == "executor_unavailable" or http_status in {404, 503}:
        return DorisResultError("backend_unavailable")
    if http_status in {408, 504}:
        return DorisResultError("query_timeout")
    if http_status in {400, 401, 403, 409, 422}:
        return DorisResultError("backend_rejected")
    return DorisResultError("query_failed", diagnostic=diagnostic)


@dataclass(frozen=True)
class ValidatedDorisResult:
    rows: list[Mapping[str, Any]]
    columns: list[Any]
    normalized_state: str

    @property
    def state(self) -> str:
        # Keep the historical internal name for existing route handlers. New
        # transport-facing code should use normalized_state.
        return "live_success" if self.rows else "no_data"


def validate_doris_result(payload: Any) -> ValidatedDorisResult:
    normalized = normalize_doris_tool_result(payload)
    if normalized.state not in {"succeeded", "empty_result"}:
        error = normalized.error if isinstance(normalized.error, Mapping) else {}
        diagnostic = (
            safe_diagnostic(error.get("diagnostic"))
            or safe_diagnostic(error.get("code"))
            or classify_query_error(error)
            or normalized.reason
        )
        raise DorisResultError(
            normalized.reason or "execution_invalid",
            missing_fields=normalized.missing_fields,
            diagnostic=diagnostic,
        )
    execution = normalized.execution
    if not isinstance(execution, Mapping):  # defensive; normalizer already checked it
        raise DorisResultError("execution_missing", missing_fields=("execution",))
    if execution.get("executor_name") != "doris":
        raise DorisResultError("not_live_result")
    return ValidatedDorisResult(
        rows=list(execution["rows"]),
        columns=list(execution["columns"]),
        normalized_state=normalized.state,
    )
