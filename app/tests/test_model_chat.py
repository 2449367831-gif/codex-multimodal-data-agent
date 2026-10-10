from copy import deepcopy

import pytest

from app.chat_api import ChatExecutionError, _project_model_results
from app.errors import UnapprovedContractResultError
from app.model_chat import present_model_answer
from app.query_outcomes import DorisResultError
from app.result_pages import ResultPageStore


def result(rows=None):
    values = [{"quantity": "12"}] if rows is None else rows
    return {
        "interaction": {"outcome": "completed", "questions": []},
        "query_execution": {"live_business_result_executed": True},
        "normalized_result_state": "succeeded" if values else "empty_result",
        "verified_business_results": [{
            "result_id": "summary", "result_title": "HJXH实发数量",
            "columns": ["quantity"], "rows": values,
            "result_columns": [{"key": "quantity", "label": "实发数量", "kind": "number", "unit": "EA"}],
        }],
    }


def present(value):
    return present_model_answer(value, request_id="r1", conversation_id="c1", context_state={},
        project_results=lambda value: _project_model_results(value, pages_store=ResultPageStore(), owner="user1"))


def test_model_result_does_not_enter_any_legacy_route_renderer():
    value = result()
    value["query_plan"] = {"route": "so07_order_details"}
    value["business_table"] = {"table_id": "sales_outbound_details", "rows": [{"forged": "999"}]}
    payload = present(value)
    assert payload["status"] == "completed"
    assert payload["business_table"]["rows"] == [{"col_1": "12"}]
    assert payload["business_table"]["columns"][0]["unit"] == "EA"


def test_empty_result_is_success_not_a_rule_fallback():
    payload = present(result([]))
    assert payload["status"] == "completed"
    assert payload["query_status"] == "empty_result"
    assert payload["business_table"]["rows"] == []
    assert "查询成功" in payload["message"]


def test_empty_result_without_a_display_table_is_still_success():
    value = result([])
    del value["verified_business_results"]
    payload = present(value)
    assert payload["status"] == "completed"
    assert payload["query_status"] == "empty_result"
    assert "business_table" not in payload
    assert "查询成功" in payload["message"]


def test_gateway_table_id_alias_is_preserved_in_public_manifest():
    value = result()
    table = value["verified_business_results"][0]
    table["table_id"] = table.pop("result_id")
    payload = present(value)
    assert payload["status"] == "completed"
    assert payload["query_status"] == "succeeded"
    assert payload["business_table"]["rows"] == [{"col_1": "12"}]


@pytest.mark.parametrize("state", ["blocked", "failed"])
def test_incomplete_model_status_does_not_hide_verified_live_result(state):
    value = result()
    value["interaction"]["outcome"] = state
    payload = present(value)
    assert payload["status"] == "completed"
    assert payload["query_status"] == "succeeded"
    assert payload["business_table"]["rows"] == [{"col_1": "12"}]


def test_health_or_model_claim_cannot_publish_rows():
    value = result()
    value["query_execution"] = {"status": "completed"}
    with pytest.raises(DorisResultError):
        present(value)


def test_unapproved_contract_keeps_its_distinct_error_before_projection():
    value = result()
    value["interaction"]["answer"] = "该口径当前尚待业务审批。"

    def reject_projection(_):
        pytest.fail("unapproved data must not reach presentation")

    with pytest.raises(UnapprovedContractResultError) as caught:
        present_model_answer(value, request_id="r", conversation_id="c", context_state={},
                             project_results=reject_projection)
    assert caught.value.http_status == 409
    assert caught.value.code == "unapproved_contract_result"


@pytest.mark.parametrize("invalid_table", [None, "invalid", [], 123])
def test_malformed_table_does_not_crash_diagnostic_logging(invalid_table):
    value = result()
    value["verified_business_results"] = [invalid_table]
    with pytest.raises(ChatExecutionError, match="查询结果表未通过校验"):
        present(value)


def test_model_questions_are_preserved_without_execution():
    questions = [{"id": "basis", "prompt": "请选择数量口径", "selection": "single",
                  "options": [{"id": "received", "label": "实收数量"}, {"id": "planned", "label": "计划数量"}]}]
    payload = present({"interaction": {"outcome": "clarification_required", "questions": deepcopy(questions)}})
    assert payload["status"] == "needs_selection"
    actual = payload["clarification"]["questions"][0]
    assert actual["id"] == questions[0]["id"]
    assert actual["prompt"] == questions[0]["prompt"]
    assert [(o["id"], o["label"]) for o in actual["options"]] == [(o["id"], o["label"]) for o in questions[0]["options"]]
    assert "business_table" not in payload
    assert [(step["id"], step["state"]) for step in payload["process"]["steps"]] == [
        ("context", "reused"), ("clarify", "pending")
    ]


def test_indistinguishable_choices_are_not_shown_as_valid_clarification():
    value = {"interaction": {"outcome": "clarification_required", "questions": [{
        "id": "basis", "prompt": "请选择数量口径", "selection": "single",
        "options": [{"id": "first", "label": "该选项"}, {"id": "second", "label": "该选项"}],
    }]}}
    with pytest.raises(DorisResultError):
        present(value)


@pytest.mark.parametrize("rows", [[], [{"quantity": "12"}]])
def test_live_discovery_does_not_override_late_clarification(rows):
    value = result(rows)
    value["interaction"] = {"outcome": "clarification_required", "questions": [{
        "id": "scope", "prompt": "请选择商品范围", "selection": "single",
        "options": [{"id": "all", "label": "全部规格"}, {"id": "one", "label": "单一规格"}],
    }]}
    def reject_projection(_):
        pytest.fail("discovery data must stay private while scope is unresolved")
    payload = present_model_answer(value, request_id="r", conversation_id="c", context_state={},
                                   project_results=reject_projection)
    assert payload["status"] == "needs_selection"
    assert payload["query_status"] == "clarification_required"
    assert "business_tables" not in payload
    assert not any(step["id"] == "validation" for step in payload["process"]["steps"])


def test_reconciliation_warning_is_visible_without_replacing_verified_rows():
    value = result()
    value["validation_warnings"] = [{"code": "reconciliation_failed", "message": "请复核对账"}]
    payload = present_model_answer(
        value, request_id="r", conversation_id="c", context_state={},
        project_results=lambda current: _project_model_results(
            current, pages_store=ResultPageStore(), owner="user1"),
    )
    assert payload["status"] == "completed"
    assert payload["query_status"] == "succeeded"
    assert payload["business_table"]["rows"] == [{"col_1": "12"}]
    assert payload["validation_warnings"] == [{"message": "请复核对账"}]


def test_legacy_rule_output_is_not_treated_as_a_model_query_result():
    value = result()
    del value["verified_business_results"]
    value["business_table"] = {"table_id": "sales_aggregate", "rows": [{"quantity": "12"}]}
    with pytest.raises(DorisResultError) as caught:
        present(value)
    assert caught.value.normalized_state == "invalid_result"
