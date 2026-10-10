from types import SimpleNamespace

import pytest

from app.query_outcomes import normalize_doris_tool_result


def structured_result(*, rows=None, **execution):
    rows = [] if rows is None else rows
    return SimpleNamespace(
        isError=True,
        structuredContent={
            "status": "succeeded",
            "execution": {
                "executor_name": "doris",
                "columns": list(rows[0]) if rows and isinstance(rows[0], dict) else [],
                "rows": rows,
                "row_count": len(rows),
                "truncated": False,
                "is_mock": False,
                **execution,
            },
        },
        content=[SimpleNamespace(text="not a business result")],
    )


def test_structured_success_wins_over_transport_error_flag():
    result = normalize_doris_tool_result(structured_result(rows=[{"qty": 2}]))
    assert result.state == "succeeded"
    assert result.execution["rows"] == [{"qty": 2}]


def test_structured_empty_rows_are_empty_result():
    assert normalize_doris_tool_result(structured_result()).state == "empty_result"


def test_truncated_result_is_not_success():
    assert normalize_doris_tool_result(structured_result(truncated=True)).state == "truncated_result"


def test_mock_result_is_invalid_result():
    assert normalize_doris_tool_result(structured_result(is_mock=True)).state == "invalid_result"


def test_missing_execution_is_invalid_with_missing_fields():
    result = normalize_doris_tool_result({"status": "succeeded"})
    assert result.state == "invalid_result"
    assert result.missing_fields == ("execution",)


def test_text_only_result_is_invalid_and_not_parsed_as_rows():
    result = normalize_doris_tool_result(
        {"isError": False, "content": [{"type": "text", "text": '{"rows":[{"qty": 9}]}'}]}
    )
    assert result.state == "invalid_result"
    assert "structuredContent.execution" in result.missing_fields


def test_transport_error_flag_without_structured_result_is_invalid():
    result = normalize_doris_tool_result({"isError": True, "error": {"code": "query_failed"}})
    assert result.state == "invalid_result"
    assert result.missing_fields == ("structuredContent", "structuredContent.execution")


def test_backend_error_uses_structured_error_code():
    result = normalize_doris_tool_result(
        {
            "structuredContent": {
                "status": "failed",
                "result_state": "unavailable",
                "error": {"code": "query_unavailable", "message": "unavailable"},
            },
            "isError": True,
        }
    )
    assert result.state == "query_unavailable"


@pytest.mark.parametrize(
    ("status", "result_state", "error_code", "expected"),
    [
        ("failed", "rejected", "sql_safety_error", "query_rejected"),
        ("failed", "failed", "query_failed", "query_failed"),
        ("failed", "unavailable", "query_unavailable", "query_unavailable"),
        ("failed", "truncated", "result_truncated", "truncated_result"),
    ],
)
def test_structured_failure_state_is_normalized_from_error_contract(
    status, result_state, error_code, expected
):
    result = normalize_doris_tool_result(
        {
            "structuredContent": {
                "status": status,
                "result_state": result_state,
                "error": {"code": error_code, "message": "private detail"},
            },
            "isError": False,
            "content": [{"type": "text", "text": "succeeded"}],
        }
    )
    assert result.state == expected
