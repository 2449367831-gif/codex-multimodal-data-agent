"""Explicit channel identifiers and product scope are request parameters."""

import pytest
import sqlglot

from app.agent_core.planning import RequestPlanner
from app.agent_core.price_group_sales import ENUM_PARENT, PriceGroupSalesHandler, compile_price_group_sales
from app.chat_api import _public_business_table
from datetime import date
from app.tests.test_price_group_sales import channels, facts, question, scope_rules
from app.tests.test_sales_detail_chat import build
from fastapi.testclient import TestClient


@pytest.mark.parametrize("parent", [
    ENUM_PARENT,
    "2c9c0da1948322ee01948bac2924000a",
    "Department_Channel_V2",
    ENUM_PARENT + "123",
])
def test_explicit_parent_is_preserved_not_corrected(parent):
    request = RequestPlanner().classify(question().replace(ENUM_PARENT, parent))
    assert request is not None and request.route == "sales_price_group"
    assert request.price_group_parent == parent


@pytest.mark.parametrize("parent", ["NEW_CHANNEL", "2c9c0da1948322ee01948bac2924000a"])
def test_parent_reaches_database_predicate_and_changes_labels(channels, parent):
    channels.executemany("INSERT INTO ywzt_ods.sys_enum VALUES (?,?,?,0)", [
        ("10", "另一分类甲", parent), ("202", "另一分类乙", parent),
    ])
    request = RequestPlanner().classify(question().replace(ENUM_PARENT, parent))
    assert request is not None

    class Execution:
        legacy_owner = "HJXH"

        def query(self, sql, *, purpose):
            assert parent in sql
            assert ENUM_PARENT not in sql
            return [dict(row) for row in channels.execute(
                sqlglot.transpile(sql, read="doris", write="sqlite")[0]
            )]

    answer = PriceGroupSalesHandler(Execution(), timezone_name="Asia/Shanghai").run(request)
    assert answer.business_table["rows"]
    assert {row["channel_name"] for row in answer.business_table["rows"]} <= {"另一分类甲", "另一分类乙"}
    public = _public_business_table(answer.business_table, live=True)
    assert public is not None
    assert any("未匹配渠道" in note for note in public["notes"])
    assert parent not in str(public)


def test_product_word_does_not_add_finished_goods_filter():
    text = question(scope="HJXH货主产品").replace("- 产成品：master_sku.sku_group = '50'\n", "")
    request = RequestPlanner().classify(text)
    assert request is not None and request.route == "sales_price_group"
    assert not request.finished_goods_only
    assert not request.scope.finished_goods


def test_parent_parameters_do_not_carry_to_next_request():
    planner = RequestPlanner()
    first = planner.classify(question().replace(ENUM_PARENT, "OTHER_CHANNEL"))
    second = planner.classify(question(scope="全部货主产成品"))
    assert first.price_group_parent == "OTHER_CHANNEL"
    assert second.price_group_parent == ENUM_PARENT
    assert second.scope.owner == "ALL"


@pytest.mark.parametrize("sku", ["QY-YwKC500G", "SKU-2026-01-02"])
def test_sku_identity_is_not_lowercased_or_parsed_as_a_date(sku):
    request = RequestPlanner().classify(question(scope=f"HJXH货主产成品 SKU {sku} "))
    assert request is not None and request.route == "sales_price_group"
    assert request.scope.sku_codes == (sku,)
    assert (request.start, request.end) == (date(2026, 9, 9), date(2026, 9, 10))


def test_all_sku_and_specific_sku_conflict_is_not_erased():
    request = RequestPlanner().classify(question(scope="HJXH所有SKU产成品 SKU Specific "))
    assert request is None


@pytest.mark.parametrize("suffix", [
    "，关联枚举父级：OTHER_CHANNEL",
    "，排除客户甲",
    "，只查渠道编码10",
])
def test_other_predicates_and_conflicting_parents_are_not_erased(suffix):
    request = RequestPlanner().classify(question() + suffix)
    assert request is None


@pytest.mark.parametrize("parent", ["", " ", "x' OR 1=1 --", "x;SELECT", "a" * 121, None])
def test_invalid_parent_stops_before_sql_execution(parent):
    with pytest.raises(ValueError):
        compile_price_group_sales(date(2026, 9, 9), date(2026, 9, 10), enum_parent=parent)


def test_unknown_parent_keeps_codes_without_fabricating_labels(channels):
    request = RequestPlanner().classify(question().replace(ENUM_PARENT, "missing_parent"))

    class Execution:
        legacy_owner = "HJXH"

        def query(self, sql, *, purpose):
            return [dict(row) for row in channels.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]

    answer = PriceGroupSalesHandler(Execution(), timezone_name="Asia/Shanghai").run(request)
    assert answer.business_table["rows"]
    assert {row["channel_name"] for row in answer.business_table["rows"]} == {"未匹配渠道"}
    assert {row["channel_code"] for row in answer.business_table["rows"]} == {"10", "202"}
    assert any("未匹配渠道" in note for note in _public_business_table(answer.business_table, live=True)["notes"])


def test_public_api_preserves_parent_and_business_notes(channels, tmp_path):
    parent = "Department_Channel_V2"
    channels.execute("INSERT INTO ywzt_ods.sys_enum VALUES (?,?,?,0)", ("10", "另一分类甲", parent))
    app, query = build(channels, tmp_path)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        body = {"conversation_id": session["conversation_id"],
                "question": question(scope="HJXH产成品JYCK").replace(ENUM_PARENT, parent)}
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "channel-parent-request-001"}
        response = client.post("/v1/chat", json=body, headers=headers)
        assert response.status_code == 200, response.json()
        result = response.json()
        assert result["result_state"] == "live_success"
        assert not result["clarification"]["required"]
        table = result["business_table"]
        assert {row["channel_name"] for row in table["rows"]} == {"另一分类甲"}
        assert len(table["notes"]) == 3
        assert "JYCK" in table["notes"][0]
        assert "未匹配渠道" in table["notes"][1]
        assert parent not in str(table)
        calls = query.calls
        assert client.post("/v1/chat", json=body, headers=headers).json() == result
        assert query.calls == calls
