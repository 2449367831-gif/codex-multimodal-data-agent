"""Only model context is bounded; verified answer tables remain complete."""

from copy import deepcopy
import json

import pytest

from app.codex_agent import _model_result_prompt
from app.model_result_context import MAX_CONTEXT_BYTES, MAX_CONTEXT_ROWS, result_context
from app.tests.test_model_execution_states import model_host, execute, turn


def payload(rows, *, columns=None, truncated=False):
    return {"status": "succeeded", "execution": {
        "columns": columns or ["code", "quantity"], "rows": rows,
        "row_count": len(rows), "truncated": truncated,
        "is_mock": False, "executor_name": "doris",
    }}


def test_small_results_preserve_every_value_and_zero_is_not_missing():
    rows = [{"code": "A", "quantity": 0}, {"code": "B", "quantity": None}]
    preview = json.loads(result_context(payload(rows)))
    assert preview["rows"] == rows
    assert preview["row_count"] == preview["preview_row_count"] == 2
    assert not preview["truncated"] and not preview["preview_truncated"]


def test_large_answer_is_not_mutated_or_copied_wholesale_into_context():
    source = payload([{"code": f"S{i}", "quantity": i} for i in range(100_000)])
    before = deepcopy(source)
    encoded = result_context(source)
    preview = json.loads(encoded)
    assert len(encoded.encode("utf-8")) <= MAX_CONTEXT_BYTES
    assert preview["rows"] == source["execution"]["rows"][:MAX_CONTEXT_ROWS]
    assert preview["row_count"] == 100_000
    assert preview["preview_row_count"] == MAX_CONTEXT_ROWS
    assert preview["preview_truncated"] and not preview["truncated"]
    assert source == before


def test_utf8_bytes_and_large_cell_values_cannot_exceed_budget():
    rows = [{"code": "合成" * MAX_CONTEXT_BYTES, "quantity": 1}, {"code": "B", "quantity": 2}]
    encoded = result_context(payload(rows))
    preview = json.loads(encoded)
    assert len(encoded.encode("utf-8")) <= MAX_CONTEXT_BYTES
    assert preview["rows"] == [] and preview["preview_truncated"]
    assert preview["row_count"] == 2


def test_batches_share_budget_and_do_not_starve_smaller_results():
    large = payload([{"code": "合成" * MAX_CONTEXT_BYTES, "quantity": 1}])
    small = payload([{"code": "B", "quantity": 2}])
    encoded = result_context(small, earlier_results=[large, small, small])
    previews = json.loads(encoded)["batch_results"]
    assert len(encoded.encode("utf-8")) <= MAX_CONTEXT_BYTES
    assert previews[0]["rows"] == []
    assert all(p["rows"] == small["execution"]["rows"] for p in previews[1:])


def test_oversized_schema_omits_whole_names_without_failing_the_answer():
    names = [f"列{i}" + "甲" * 159 for i in range(128)]
    source = payload([], columns=names)
    encoded = result_context(source)
    preview = json.loads(encoded)
    assert len(encoded.encode("utf-8")) <= MAX_CONTEXT_BYTES
    assert preview["column_count"] == len(names)
    assert preview["columns"] == names[:len(preview["columns"])]
    assert preview["columns_preview_truncated"]
    assert source["execution"]["columns"] == names


@pytest.mark.parametrize("truncated", [False, True])
def test_empty_and_truncated_query_states_are_not_hidden(truncated):
    preview = json.loads(result_context(payload([], truncated=truncated)))
    assert preview["row_count"] == preview["preview_row_count"] == 0
    assert preview["truncated"] is truncated
    assert preview["preview_truncated"] is False


def test_private_prompt_distinguishes_preview_from_business_completeness():
    prompt = _model_result_prompt(payload([{"code": "A", "quantity": 1}]), question="原始请求")
    assert "CURRENT_REQUEST_CAPSULE:\n原始请求\nEND_CURRENT_REQUEST_CAPSULE" in prompt
    assert "preview_truncated" in prompt and "不得用预览推断总计" in prompt
    assert "不能因预览受限重查" in prompt


def test_host_delivers_all_rows_even_when_model_only_sees_preview(model_host):
    thread, bridge = model_host
    plan = {"outcome": "query_plan", "kind": "business", "purpose": "synthetic directory",
            "sql": "SELECT wms_code AS code, wms_name AS label FROM ywzt_ods.master_wms",
            "result_id": "directory", "result_title": "仓库清单",
            "result_columns": [{"key": "code", "label": "仓库编码"}, {"key": "label", "label": "仓库名称"}]}
    rows = [{"code": f"S{i}", "label": "合成仓"} for i in range(501)]
    thread.run.side_effect = [turn(plan), turn({"outcome": "completed", "answer": "已查询仓库清单。",
                                               "result_ids": ["directory"]})]
    bridge.query.return_value = payload(rows, columns=["code", "label"])
    result = execute()
    assert result["result_state"] == "live_success"
    assert bridge.query.call_count == 1 and thread.run.call_count == 2
    assert '"row_count":501' in thread.run.call_args_list[1].args[0]
    assert '"preview_row_count":40' in thread.run.call_args_list[1].args[0]
    assert '"code":"S500"' not in thread.run.call_args_list[1].args[0]
    assert result["verified_business_results"][0]["rows"] == rows
