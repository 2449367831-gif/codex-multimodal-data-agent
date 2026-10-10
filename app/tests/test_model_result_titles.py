import json

import pytest

from app.codex_agent import _model_query_plans
from app.model_query_contract import result_identity
from app.public_table_text import business_result_title
from app.tests.test_model_execution_states import execute, model_host, turn
from app.tests.test_model_query_contract import business, payload, validation


@pytest.mark.parametrize("title", ["HJXH昨日实际销售出库Top20 SKU", "GZHYC-777库存", "GMV月度汇总"])
def test_business_identifiers_do_not_reject_result_titles(title):
    assert result_identity({"result_id": "summary", "result_title": title})["result_title"] == title


@pytest.mark.parametrize("title", ["SELECT qty FROM secret", "ywzt_ods.master_wms", "数据库密码",
                                   "<script>alert(1)</script>", "执行日志", "数量\n内部", "x" * 121])
def test_private_or_unsafe_titles_still_rejected(title):
    assert business_result_title(title) is None


def test_business_code_title_reaches_execution_and_verified_delivery(model_host):
    thread, bridge = model_host
    plan = {**business(), "result_id": "summary", "result_title": "HJXH仓库资料"}
    batch = {"outcome": "query_plan_batch", "plans": [plan, validation()],
             "completion": {"result_ids": ["summary"]}}
    assert _model_query_plans(json.dumps(batch)) is not None
    thread.run.return_value = turn(batch)
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]), payload([{"conflicts": 0}])]
    result = execute()
    assert result["result_state"] == "live_success"
    assert result["interaction"]["outcome"] == "completed"
    assert thread.run.call_count == 1
    assert bridge.query.call_count == 2


def test_unrepairable_plan_is_not_reported_as_business_clarification(model_host):
    thread, bridge = model_host
    thread.run.return_value = turn({**business(), "result_columns": []})
    result = execute()
    assert result["normalized_result_state"] == "invalid_result"
    assert result["query_error"]["code"] == "query_plan_invalid"
    assert result["interaction"] == {"outcome": "failed", "questions": []}
    assert "尚未执行" in result["query_error"]["message"]
    assert thread.run.call_count == 2
    bridge.query.assert_not_called()
