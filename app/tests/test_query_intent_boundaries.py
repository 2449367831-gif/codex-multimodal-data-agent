"""Planning guidance must not consume requests for actual partitioned data."""

import pytest
from fastapi.testclient import TestClient
from datetime import date, datetime, timedelta

from app.agent_core.planning import RequestPlanner
from app.agent_core.sales_detail_planning import sales_detail_request
from app.request_guidance import large_result_guidance
from app.request_intent import assess_request
from app.tests.test_sales_aggregation import facts
from app.tests.test_sales_detail_chat import build


@pytest.mark.parametrize("question", [
    "查询近14天实际销售出库明细，按月分批查询",
    "按月拆分查询近一年实际销售出库明细，并返回表格数据",
    "结果截断了，继续按仓库查询销售明细",
    "大结果集按SKU拆分查询，把结果给我",
    "查询近14天实际销售出库明细，超限时说明怎么处理",
    "先给出出库明细拆分查询方案，然后执行并返回数据",
    "不要只给大结果集处理方案，按月把出库明细查出来",
    "按月分段查询销售明细并说明各分段的结果",
    "后端返回结果达到行数上限。请说明拆分查询方案，再按月查询近14天实际销售出库明细",
    "请说明按仓库拆分查询出库明细的方案，并导出CSV",
    "请说明大结果集处理方案，按月查询近14天实际销售出库明细",
])
def test_execution_requests_are_never_explanation_only(question):
    assert large_result_guidance(question) is None
    assert assess_request(question).kind != "explanation"


@pytest.mark.parametrize("question", [
    "请说明大结果集按月拆分查询的方案，不要执行",
    "出库明细结果截断时如何处理？",
    "只给出按仓库分段查询销售明细的方案",
])
def test_pure_planning_requests_need_no_database(question):
    guidance = large_result_guidance(question)
    assert guidance is not None
    assert assess_request(question).kind == "explanation"
    assert "去重" in guidance["text"]
    assert "不能直接相加" in guidance["text"]


@pytest.mark.parametrize("question", [
    "查询近14天实际销售出库明细，按月分批查询",
    "按月拆分查询近14天实际销售出库明细",
    "近14天实际销售出库明细，按月份分段查询，用表格展示",
])
def test_month_partition_requests_execute_reviewed_reader(facts, tmp_path, question):
    app, query = build(facts, tmp_path)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        response = client.post("/v1/chat", json={
            "question": question, "conversation_id": session["conversation_id"],
        }, headers={
            "X-CSRF-Token": session["csrf_token"],
            "Idempotency-Key": "monthly-details-intent-001",
        })
        assert response.status_code == 200, response.json()
        result = response.json()
        assert result["result_state"] == "live_success"
        assert query.calls > 0
        assert result["business_table"]["rows"]
        assert "guidance" not in result
        before = query.calls
        replay = client.post("/v1/chat", json={
            "question": question, "conversation_id": session["conversation_id"],
        }, headers={
            "X-CSRF-Token": session["csrf_token"],
            "Idempotency-Key": "monthly-details-intent-001",
        })
        assert replay.json() == result
        assert query.calls == before


@pytest.mark.parametrize("days", [7, 14, 31, 365])
@pytest.mark.parametrize("modifier", ["按月分批查询", "按月份分段查询", "按月拆分查询"])
def test_month_modifier_preserves_calendar_and_every_scope(days, modifier):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    request = planner.classify(f"{modifier}近{days}天HJXH长沙黄兴SKU QY-2026-01-02实际销售出库明细")
    assert request is not None and request.route == "sales_outbound_details"
    assert (request.start, request.end) == (date(2026, 9, 10) - timedelta(days=days), date(2026, 9, 10))
    assert request.scope.owner == "HJXH"
    assert request.scope.warehouse_codes == ("10114790",)
    assert request.scope.sku_codes == ("QY-2026-01-02",)
    following = planner.classify(f"近{days}天实际销售出库明细，{modifier}")
    assert following.scope.owner == "ALL"
    assert not following.scope.sku_codes and not following.scope.warehouse_codes


@pytest.mark.parametrize("question", [
    "按月分批查询近14天实际销售出库明细，排除客户甲",
    "近14天实际销售出库明细，按月分批查询数量大于5",
    "近14天实际销售出库明细，按仓库拆分查询",
    "近14天所有出库明细，按月分批查询",
    "近14天实际销售出库明细，按SKU分段查询",
])
def test_month_modifier_does_not_drop_unimplemented_conditions(question):
    assert sales_detail_request(question, planner=RequestPlanner()) is None
