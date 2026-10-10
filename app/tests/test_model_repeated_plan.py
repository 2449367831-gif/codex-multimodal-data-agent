"""One cached-step recovery must not rerun SQL or drop uncompleted work."""

from copy import deepcopy

import pytest

from app.tests.test_model_execution_states import model_host, execute, turn
from app.tests.test_model_query_contract import business, payload
from app.codex_mcp_bridge import CodexMcpBridgeError


def test_repeated_metadata_can_advance_to_the_business_query(model_host):
    thread, bridge = model_host
    metadata = {"outcome": "query_plan", "kind": "metadata", "sql": "SELECT column_name FROM information_schema.columns",
                "purpose": "schema"}
    thread.run.side_effect = [turn(metadata), turn(metadata), turn(business()),
                             turn({"outcome": "completed", "answer": "已查询仓库资料"})]
    bridge.query.side_effect = [payload([{"column_name": "wms_code"}]), payload([{"location": "A", "label": "合成仓"}])]
    result = execute()
    assert result["result_state"] == "live_success"
    assert bridge.query.call_count == 2
    assert result["query_execution"]["metadata_query_call_count"] == 1
    prompt = thread.run.call_args_list[2].args[0]
    assert "HOST_PLAN_REPLAY" in prompt and "CURRENT_REQUEST_CAPSULE:\n查询仓库资料" in prompt
    assert "SELECT" not in prompt.split("HOST_COMPLETED_STEP:")[1]


def test_repeated_named_business_table_can_finish_without_reexecution(model_host):
    thread, bridge = model_host
    plan = {**business(), "result_id": "directory", "result_title": "仓库清单"}
    rows = [{"location": "A", "label": "合成仓"}]
    thread.run.side_effect = [turn(plan), turn(plan), turn({"outcome": "completed", "answer": "已查询仓库清单",
                                                         "result_ids": ["directory"]})]
    bridge.query.return_value = payload(rows)
    result = execute()
    assert result["result_state"] == "live_success" and result["host_failure"] is None
    assert result["verified_business_results"][0]["rows"] == rows
    assert bridge.query.call_count == 1


@pytest.mark.parametrize("diagnostic", ["unknown_column", "transport_error"])
def test_failed_queries_are_not_replayed_as_successful(model_host, diagnostic):
    thread, bridge = model_host
    thread.run.return_value = turn(business())
    bridge.query.side_effect = CodexMcpBridgeError("query_failed", diagnostic=diagnostic)
    result = execute()
    assert result["result_state"] == "failed"
    assert bridge.query.call_count == 1
    assert thread.run.call_count == (2 if diagnostic == "unknown_column" else 1)
    assert all("HOST_PLAN_REPLAY" not in c.args[0] for c in thread.run.call_args_list)


def test_repeated_cached_step_cannot_erase_a_pending_batch_member(model_host):
    thread, bridge = model_host
    plan = business()
    next_plan = business("SELECT DISTINCT wms_code AS location, wms_name AS label FROM ywzt_ods.master_wms")
    thread.run.side_effect = [turn(plan), turn({"outcome": "query_plan_batch", "plans": [plan, next_plan]})]
    bridge.query.return_value = payload([{"location": "A", "label": "合成仓"}])
    result = execute()
    assert result["result_state"] == "failed"
    assert bridge.query.call_count == 1 and thread.run.call_count == 2


@pytest.mark.parametrize("mutation", ["identity", "validation", "label"])
def test_repeated_sql_with_changed_contract_is_not_the_same_completed_step(model_host, mutation):
    thread, bridge = model_host
    plan = business()
    changed = deepcopy(plan)
    if mutation == "identity":
        changed.update(result_id="another", result_title="另一个结果")
    elif mutation == "validation":
        changed.pop("result_columns")
        changed.update(kind="validation", checks=[{"column": "location", "operator": "eq", "value": 0}])
    else:
        changed["result_columns"][0]["label"] = "货主编码"
    thread.run.side_effect = [turn(plan), turn(changed)]
    bridge.query.return_value = payload([{"location": "A", "label": "合成仓"}])
    result = execute()
    assert result["result_state"] == "failed" and bridge.query.call_count == 1
    assert thread.run.call_count == 2


def test_repeated_plan_recovery_remains_bounded(model_host):
    thread, bridge = model_host
    thread.run.return_value = turn(business())
    bridge.query.return_value = payload([{"location": "A", "label": "合成仓"}])
    result = execute()
    assert result["result_state"] == "failed"
    assert thread.run.call_count == 3 and bridge.query.call_count == 1
    assert sum("HOST_PLAN_REPLAY" in c.args[0] for c in thread.run.call_args_list) == 1
