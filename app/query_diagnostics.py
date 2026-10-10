"""Data-free diagnostic vocabulary shared by the query host and model planner."""

from collections.abc import Mapping


DIAGNOSTICS = frozenset({
    "unknown_column", "unknown_table", "ambiguous_column", "syntax_error",
    "unsupported_function", "type_mismatch", "resource_limit", "permission_denied",
    "query_timeout", "transport_error", "result_projection_invalid",
    "reconciliation_failed",
})


def safe_diagnostic(value):
    return value if isinstance(value, str) and value in DIAGNOSTICS else None


def safe_host_failure(value):
    value = value if isinstance(value, Mapping) else {}
    stage = value.get("stage")
    stages = {"query", "plan_batch", "repeated_plan", "model_response", "plan_contract", "answer_contract", "step_budget"}
    step = value.get("step")
    return {"stage": stage if isinstance(stage, str) and stage in stages else "unknown",
            "diagnostic": safe_diagnostic(value.get("diagnostic")) or "unspecified",
            "step": step if type(step) is int and 0 <= step <= 128 else 0}


def classify_query_error(details):
    if not isinstance(details, Mapping):
        return None
    known = safe_diagnostic(details.get("diagnostic"))
    if known:
        return known
    code = details.get("db_error_code")
    code = code if type(code) is int else None
    codes = {
        1054: "unknown_column", 1146: "unknown_table", 1052: "ambiguous_column",
        1064: "syntax_error", 1305: "unsupported_function",
        1044: "permission_denied", 1045: "permission_denied", 1142: "permission_denied",
        1143: "permission_denied", 1227: "permission_denied",
        2003: "transport_error", 2006: "transport_error", 2013: "transport_error",
    }
    if code in codes:
        return codes[code]
    # Inspect text locally but emit only fixed codes, never identifiers, values
    # or message fragments. Doris often uses generic 1105 for analysis errors.
    message = str(details.get("message") or "")[:16000].casefold()
    patterns = (
        ("permission_denied", ("access denied", "permission denied", "not authorized")),
        ("unknown_column", ("unknown column", "cannot resolve column", "could not resolve column")),
        ("unknown_table", ("unknown table", "table does not exist")),
        ("ambiguous_column", ("ambiguous column", "column is ambiguous")),
        ("unsupported_function", ("no matching function", "unknown function", "function not found")),
        ("type_mismatch", ("type mismatch", "cannot cast", "incompatible type")),
        ("resource_limit", ("memory limit", "mem limit", "resource limit")),
        ("query_timeout", ("query timeout", "query timed out", "execution timeout")),
        ("syntax_error", ("syntax error", "syntaxerror", "parse error")),
    )
    return next((category for category, markers in patterns if any(m in message for m in markers)), None)
