from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime

import pytest
import sqlglot

from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRuntimeError
from app.agent_core.outbound_link_planning import link_clarification
from app.agent_core.outbound_links import OutboundLinkPlan, compile_outbound_links, reconcile_outbound_links
from app.chat_api import _public_business_table
from app.codex_agent import reviewed_clarification_for_question
from app.query_scope import QueryScope
from app.tests.test_outbound_links import links, execute


QUESTION = "查询一张出库单的关联链路，返回出库单号、销售订单号、来源单号、订单来源编码和订单类型，并解释这些字段分别来自哪张表。"


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr("app.agent_core.outbound_link_planning.active_link_contract", lambda _: True)


def planner():
    return RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))


def test_exact_code_and_owner_do_not_become_dates_or_sku_scope():
    request = planner().classify("查询货主 B 出库单号 `HJXH-2026-08-03-2C-SO07` 的关联链路")
    assert request and request.route == "sales_outbound_link"
    assert request.outbound_link_plan.outbound_codes == ("HJXH-2026-08-03-2C-SO07",)
    assert request.scope == QueryScope(owner="B", unit="none")
    assert request.start is None and request.end is None


def test_all_dates_and_scope_rebuild():
    p = planner()
    assert p.classify("查询昨天货主 A 出库单号 OUT-1 的关联链路").scope.owner == "A"
    request = p.classify("查询出库单号 OUT-1 的关联链路")
    assert request.scope.owner == "ALL" and request.start is None
    assert request.outbound_link_plan.owner == "ALL"


def test_one_card_for_missing_identity_and_no_repeated_choice():
    p = planner()
    assert p.classify(QUESTION) is None
    card = reviewed_clarification_for_question(QUESTION)
    assert card and [q["id"] for q in card["questions"]] == ["outbound_link_scope"]
    assert {o["id"] for o in card["questions"][0]["options"]} == {"outbound_link_latest", "outbound_link_all"}
    for option in ("outbound_link_latest", "outbound_link_all"):
        request = p.plan_selected(QUESTION, [{"question_id": "outbound_link_scope", "option_id": option}])
        assert request and request.outbound_link_plan.latest == (option == "outbound_link_latest")
        assert request.scope.owner == "ALL" and request.start is None


def test_selected_relative_date_uses_original_request_time():
    from zoneinfo import ZoneInfo
    request = planner().plan_selected(QUESTION + "昨天", [{"question_id": "outbound_link_scope", "option_id": "outbound_link_latest"}],
        reference_time=datetime(2026, 9, 10, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert request.start == date(2026, 9, 9) and request.end == date(2026, 9, 10)


@pytest.mark.parametrize("suffix", ["仅限长沙黄兴", "只要成品", "统计金额", "数量大于10", "排除订单类型SO07",
                                        "2026-13-01", "近24小时", "仓库 W1", "客户甲", "删除这些单据"])
def test_unsupported_predicates_never_become_broader_link_query(suffix):
    question = "查询出库单号 OUT-1 的关联链路，" + suffix
    try:
        request = planner().classify(question)
    except SemanticRuntimeError:
        return
    assert request is None


def test_unpublished_contract_does_not_route_or_offer_executable_choices(monkeypatch):
    monkeypatch.setattr("app.agent_core.outbound_link_planning.active_link_contract", lambda _: False)
    assert planner().classify("查询出库单号 OUT-1 的关联链路") is None
    assert link_clarification(QUESTION, contract_root=None) is None


def test_invalid_date_does_not_advertise_an_executable_choice():
    assert link_clarification(QUESTION + "2026-13-01", contract_root=None) is None


def test_extra_selected_decisions_cannot_silently_change_scope():
    assert planner().plan_selected(QUESTION, [
        {"question_id":"outbound_link_scope", "option_id":"outbound_link_all"},
        {"question_id":"output_grain", "option_id":"sku"},
    ]) is None


class Query:
    def __init__(self, db):
        self.db = db
        self.calls = 0
        self.mutate = None

    def query(self, *, sql, purpose):
        self.calls += 1
        rows = [dict(r) for r in self.db.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
        if self.calls == 2 and self.mutate:
            self.mutate(rows)
        return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
            "rows": rows, "row_count": len(rows), "truncated": False}}


def test_runtime_and_public_columns_keep_all_provenance_and_nulls(links):
    client = Query(links)
    runtime = SemanticQueryRuntime(query_client=client)
    result = runtime.run_if_supported("查询全部出库单的关联链路")
    assert result["result_state"] == "live_success" and client.calls == 2
    assert result["query_plan"]["grain"] == "owner+outbound+detail_source"
    assert result["query_plan"]["scope"]["unit"] == "none"
    public = _public_business_table(result["business_table"], live=True)
    assert public and len(public["columns"]) == 11 and len(public["notes"]) == 3
    missing = next(row for row in public["rows"] if row["outbound_code"] == "OUT-2")
    assert missing["linked_sales_order_code"] is None and missing["relationship_state"] == "未匹配销售订单"
    matched = next(row for row in public["rows"] if row["cargo_owner"] == "A" and row["outbound_code"] == "OUT-1")
    assert matched["source_order_code"] == "HEADER-A" and matched["detail_source_order_code"] in {"LINE-A1", "LINE-A2"}
    assert matched["referenced_order_type"] == "JYCK" and matched["order_type"] == "SO01"
    assert not any("header_variants" in row for row in public["rows"])


def test_latest_document_is_explicit_deterministic_and_preserves_all_sources(links):
    plan = OutboundLinkPlan(start=date(2026, 9, 10), end=date(2026, 9, 11), latest=True)
    result = reconcile_outbound_links(execute(links, plan), plan)
    assert result.document_count == 1 and result.rows[0]["outbound_code"] == "OUT-3"
    links.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET ship_at='2026-09-10 23:00:00' WHERE outbound_code='OUT-1'")
    result = reconcile_outbound_links(execute(links, plan), plan)
    assert result.document_count == 1 and len(result.rows) == 2
    assert {row["cargo_owner"] for row in result.rows} == {"A"}


def test_latest_rejects_missing_time_and_never_means_arbitrary_first_row(links):
    plan = OutboundLinkPlan(latest=True)
    rows = execute(links, plan)
    rows[0]["ship_at"] = None
    with pytest.raises(SemanticRuntimeError):
        reconcile_outbound_links(rows, plan)
    links.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET ship_at=NULL")
    assert reconcile_outbound_links(execute(links, plan), plan).document_count == 0


def test_changed_second_read_and_unexpected_scope_fail_before_presentation(links):
    client = Query(links)
    client.mutate = lambda rows: rows[0].update(source_order_code="CHANGED")
    runtime = SemanticQueryRuntime(query_client=client)
    with pytest.raises(SemanticRuntimeError):
        runtime.run_if_supported("查询出库单号 OUT-1 的关联链路")
    request = planner().classify("查询出库单号 OUT-1 的关联链路")
    with pytest.raises(SemanticRuntimeError):
        runtime.execute_plan(replace(request, scope=QueryScope(owner="B", unit="none")), question="query")


def test_empty_result_keeps_successful_no_data_state(links):
    result = SemanticQueryRuntime(query_client=Query(links)).run_if_supported("查询出库单号 NONE 的关联链路")
    assert result["result_state"] == "no_data" and result["business_table"]["rows"] == []


def test_unsafe_public_value_rejects_whole_table(links):
    result = SemanticQueryRuntime(query_client=Query(links)).run_if_supported("查询出库单号 OUT-1 的关联链路")
    raw = deepcopy(result["business_table"])
    raw["rows"][-1]["source_order_code"] = "SELECT secret FROM private.table"
    assert _public_business_table(raw, live=True) is None
