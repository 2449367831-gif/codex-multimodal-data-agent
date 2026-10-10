from __future__ import annotations

import json
from types import SimpleNamespace

from app.codex_agent import (
    _model_query_plans,
    _model_query_retry_prompt,
    _model_result_prompt,
    _pending_query_repair_prompt,
    _repair_plans_preserve_contract,
    _same_query_contract,
    _model_value,
    _model_plan_fingerprint,
    _is_metadata_only_sql,
)
from app.query_lifecycle import QueryLifecycle
from app.query_outcomes import DorisResultError


def turn(value):
    return SimpleNamespace(status="completed", final_response=json.dumps(value), items=[])


def payload(rows):
    return {
        "status": "succeeded",
        "execution": {
            "executor_name": "doris",
            "is_mock": False,
            "truncated": False,
            "rows": rows,
            "row_count": len(rows),
            "columns": list(rows[0]) if rows else [],
        },
    }


def make_lifecycle(turns, responses):
    class Bridge:
        def __init__(self):
            self.calls = []

        def query(self, *, sql, purpose):
            self.calls.append(sql)
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    bridge = Bridge()

    def run(_thread, prompt, **_kwargs):
        value = turns.pop(0)
        return turn(value)

    initial = turns.pop(0)
    plans = _model_query_plans(json.dumps(initial))
    return QueryLifecycle(
        thread=object(), bridge=bridge, result=turn(initial), result_items=[], plans=plans,
        question="查询仓库资料", max_steps=12, turn_runner=run,
        plan_parser=_model_query_plans, plan_fingerprint=_model_plan_fingerprint,
        same_contract=_same_query_contract,
        repair_plans_preserve_contract=_repair_plans_preserve_contract,
        retry_prompt=_model_query_retry_prompt, contract_repair_prompt=lambda *_: "repair",
        render_prompt=_model_result_prompt, repeated_prompt=lambda *_: "repeat",
        pending_prompt=_pending_query_repair_prompt, effort=object(), model="test",
        sandbox=object(), is_metadata=_is_metadata_only_sql, status_value=_model_value,
    ), bridge


def business_plan():
    return {
        "outcome": "query_plan", "kind": "business", "result_id": "warehouse",
        "result_title": "仓库", "result_columns": [
            {"key": "location", "label": "仓库", "kind": "text"},
        ], "sql": "SELECT wms_code AS location FROM ywzt_ods.master_wms",
        "purpose": "warehouse result",
    }


def test_failed_business_cannot_be_replaced_by_metadata_only():
    lifecycle, bridge = make_lifecycle(
        [
            {"outcome": "query_plan", "kind": "metadata", "sql": "SELECT column_name FROM information_schema.columns", "purpose": "probe"},
            {"outcome": "completed", "answer": "已完成"},
        ],
        [DorisResultError("query_timeout")],
    )
    result = lifecycle.run()
    assert result.query_error["result_state"] == "failed"
    assert result.bridge_calls[0]["status"] == "failed"
    assert len(bridge.calls) == 1


def test_successful_repair_clears_failure_and_publishes_one_verified_table():
    plan = business_plan()
    repaired = {**plan, "sql": "SELECT DISTINCT wms_code AS location FROM ywzt_ods.master_wms"}
    lifecycle, bridge = make_lifecycle(
        [
            {"outcome": "query_plan", **plan},
            {"outcome": "query_plan", **repaired},
            {"outcome": "completed", "answer": "已完成"},
        ],
        [
            DorisResultError("query_failed", diagnostic="unknown_column"),
            payload([{"location": "001"}]),
        ],
    )
    result = lifecycle.run()
    assert result.query_error is None
    assert result.last_verified_business["rows"] == [{"location": "001"}]
    assert len(bridge.calls) == 2


def test_repeated_plan_uses_one_query_and_never_loops():
    plan = business_plan()
    lifecycle, bridge = make_lifecycle(
        [plan, plan, {"outcome": "completed", "answer": "已完成"}],
        [payload([{"location": "001"}])],
    )
    result = lifecycle.run()
    assert result.query_error is None
    assert len(bridge.calls) == 1


def test_contract_repair_failure_is_classified_after_verified_query():
    plan = business_plan()
    lifecycle, bridge = make_lifecycle(
        [plan, {"outcome": "query_plan", "purpose": "malformed format-only response"}],
        [payload([{"location": "001"}])],
    )
    result = lifecycle.run()
    assert result.query_error["result_state"] == "failed"
    assert result.host_failure["stage"] == "plan_contract"
    assert len(bridge.calls) == 1


def test_failed_validation_clears_stale_batch_completion_before_rendering():
    summary = {
        "outcome": "query_plan", "kind": "business", "result_id": "summary",
        "result_title": "汇总", "result_columns": [{"key": "total", "label": "总数"}],
        "sql": "SELECT COUNT(*) AS total FROM ywzt_ods.master_wms",
        "purpose": "summary",
    }
    detail = business_plan()
    validation = {
        "outcome": "query_plan", "kind": "validation",
        "sql": "SELECT COUNT(*) AS conflicts FROM ywzt_ods.master_wms",
        "purpose": "validation",
        "checks": [{"column": "conflicts", "operator": "eq", "value": 0}],
    }
    repaired_validation = {**validation, "sql": "SELECT COUNT(*) AS conflicts FROM ywzt_ods.master_wms WHERE 1 = 1"}
    batch = {"outcome": "query_plan_batch", "plans": [summary, detail, validation],
             "completion": {"result_ids": ["summary", "warehouse"]}}
    lifecycle, bridge = make_lifecycle(
        [batch, {"outcome": "query_plan", **repaired_validation}, {"outcome": "completed", "answer": "已完成"}],
        [payload([{"total": 1}]), payload([{"location": "001"}]),
         DorisResultError("query_timeout"), payload([{"conflicts": 0}])],
    )
    result = lifecycle.run()
    assert result.query_error is not None
    assert result.query_error["normalized_state"] == "query_failed"
    assert result.final_contract is None
    assert len(bridge.calls) == 3


def test_failed_query_can_return_selectable_clarification_instead_of_generic_failure():
    lifecycle, bridge = make_lifecycle(
        [
            {"outcome": "query_plan", **business_plan()},
            {"outcome": "clarification_required", "questions": [{
                "id": "scope", "prompt": "请选择范围", "selection": "single",
                "options": [{"id": "all", "label": "全部"}],
            }]},
        ],
        [DorisResultError("query_failed")],
    )
    result = lifecycle.run()
    assert result.query_error is not None
    assert result.query_error["normalized_state"] == "query_failed"
    assert result.answer_results.manifest() == []
    assert len(bridge.calls) == 1


def test_reconciliation_mismatch_preserves_verified_business_bundle_with_warning():
    validation = {
        "outcome": "query_plan", "kind": "validation",
        "sql": "SELECT COUNT(*) AS conflicts FROM ywzt_ods.master_wms",
        "purpose": "check unique warehouse keys",
        "checks": [{"column": "conflicts", "operator": "eq", "value": 0}],
    }
    lifecycle, bridge = make_lifecycle(
        [{"outcome": "query_plan_batch", "plans": [business_plan(), validation],
          "completion": {"result_ids": ["warehouse"]}}],
        [payload([{"location": "001"}]), payload([{"conflicts": 2}])],
    )
    result = lifecycle.run()
    assert result.query_error is None
    assert result.host_failure is None
    assert result.final_contract["result_ids"] == ["warehouse"]
    assert result.validation_warnings[0]["code"] == "reconciliation_failed"
    assert result.validation_query_count == 0
    assert [call["status"] for call in result.bridge_calls] == ["completed", "failed"]
    assert len(bridge.calls) == 2


def test_failed_preflight_validation_never_skips_to_business():
    validation = {
        "outcome": "query_plan", "kind": "validation",
        "sql": "SELECT COUNT(*) AS conflicts FROM ywzt_ods.master_wms",
        "purpose": "validation",
        "checks": [{"column": "conflicts", "operator": "eq", "value": 0}],
    }
    lifecycle, bridge = make_lifecycle(
        [validation, business_plan(), {"outcome": "completed", "answer": "已完成"}],
        [
            DorisResultError("reconciliation_failed", diagnostic="reconciliation_failed"),
            payload([{"location": "001"}]),
        ],
    )
    result = lifecycle.run()
    assert result.query_error["failure_reason"] == "reconciliation_failed"
    assert result.last_verified_business is None
    assert len(bridge.calls) == 1


def test_unavailable_retry_does_not_relabel_original_failed_call():
    lifecycle, bridge = make_lifecycle(
        [business_plan(), {"outcome": "completed", "answer": "已完成"}],
        [DorisResultError("backend_unavailable"), payload([{"location": "001"}])],
    )
    result = lifecycle.run()
    assert result.query_error is None
    assert [call["status"] for call in result.bridge_calls] == ["failed", "completed"]
    assert result.business_states == ["live_success"]
    assert len(bridge.calls) == 2


def test_empty_business_result_requires_diagnostic_before_validation():
    empty = {
        "outcome": "query_plan", "kind": "business",
        "sql": "SELECT sku_code FROM ywzt_ods.master_sku WHERE sku_name = '简称'",
        "purpose": "resolve the requested entity",
    }
    validation = {
        "outcome": "query_plan", "kind": "validation",
        "sql": "SELECT COUNT(*) AS conflicts FROM ywzt_ods.master_wms",
        "purpose": "validation",
        "checks": [{"column": "conflicts", "operator": "eq", "value": 0}],
    }
    diagnostic = {
        "outcome": "query_plan", "kind": "business",
        "sql": "SELECT sku_code FROM ywzt_ods.master_sku WHERE sku_name LIKE '%简称%'",
        "purpose": "resolve entity candidates",
    }
    fact = business_plan()
    lifecycle, bridge = make_lifecycle(
        [empty, validation, diagnostic, fact, {"outcome": "completed", "answer": "已完成"}],
        [payload([]), payload([{"sku_code": "S1"}]), payload([{"location": "001"}])],
    )
    result = lifecycle.run()
    assert result.query_error is None
    assert result.host_failure is None
    assert result.validation_query_count == 0
    assert result.last_verified_business["rows"] == [{"location": "001"}]
    assert len(bridge.calls) == 3
