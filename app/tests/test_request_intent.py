"""Intent tests use request text and contracts, never observed business answers."""

from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.request_intent import assess_request
from app.codex_agent import fallback_interaction_for_question
from app.chat_api import _should_offer_selectable_recovery
from app.agent_core.planning import RequestPlanner


@pytest.mark.parametrize("question,kind,domain", [
    ("查询名称包含‘沈阳’的所有仓库，返回仓库编码、仓库名称、仓库类型", "lookup", "warehouse"),
    ("查名称包含杭州的仓库", "lookup", "warehouse"),
    ("列出全部仓库编码和名称", "lookup", "warehouse"),
    ("查询SKU A001的主数据名称和箱规", "lookup", "sku"),
    ("查询客户名称包含测试的客户编码", "lookup", "customer"),
    ("昨天各仓库实际销售出库", "data_query", "sales"),
    ("今天SO07订单的请求数量", "data_query", "sales_order"),
    ("查询所有仓库实时库存", "data_query", "inventory"),
    ("查询DMS积分订单及城市经理", "data_query", "dms"),
    ("请解释销售占用和调拨占用的识别规则", "explanation", "inventory"),
    ("这次采购在途口径变更有哪些字段受影响", "change_assessment", "inventory"),
    ("请排查在途翻倍的原因", "diagnostic", "inventory"),
    ("将MySQL查询改写成Doris，只保留只读查询", "sql_conversion", "unknown"),
    ("昨天出库量，请列出需要确认的选项，不要猜", "clarification", "sales"),
    ("你好", "unknown", "unknown"),
])
def test_request_kind_is_separate_from_metric(question, kind, domain):
    intent = assess_request(question)
    assert intent.kind == kind
    assert domain in intent.domains
    if kind == "lookup":
        assert intent.requires_time_window is False


@pytest.mark.parametrize("question", [
    "查询名称包含沈阳的仓库，返回仓库编码、仓库名称、仓库类型",
    "查询SKU A001的主数据",
    "列出客户编码和客户名称",
    "请解释采购在途和调拨在途的区别",
    "你好",
])
def test_unrelated_requests_never_receive_generic_metric_or_sales_window_card(question):
    result = fallback_interaction_for_question(question)
    assert result["outcome"] != "clarification_required"
    assert result.get("questions", []) == []


@pytest.mark.parametrize("state", ["blocked", "failed", "completed"])
def test_technical_failure_is_not_a_missing_business_decision(state):
    assert not _should_offer_selectable_recovery(
        {"final_response": "", "query_execution": {"live_business_result_executed": False}},
        {"outcome": state, "questions": []},
        error_reason="no_live_query",
    )


@pytest.mark.parametrize("question,start,end", [
    ("2026-09-01至2026-09-10 0点前的销售出库", date(2026, 9, 1), date(2026, 9, 10)),
    ("2026年9月1日至2026年9月10日00:00的销售出库", date(2026, 9, 1), date(2026, 9, 10)),
    ("2026年9月1日至2026年9月10日销售出库", date(2026, 9, 1), date(2026, 9, 11)),
    ("昨天销售出库", date(2026, 9, 9), date(2026, 9, 10)),
])
def test_explicit_midnight_end_is_exclusive(question, start, end):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert planner._source_interval(question) == (start, end)


def test_full_acceptance_scope_is_registered_without_numeric_answer_keys():
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "tests/acceptance/complex-query-cases.json").read_text(encoding="utf-8"))
    cases = manifest["cases"]
    assert [case["id"] for case in cases] == [f"T{i:02d}" for i in range(1, 41)]
    assert all(case["question"] for case in cases)
    assert all("expected_rows" not in case and "expected_value" not in case for case in cases)
