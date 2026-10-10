"""Scope declarations and opaque identifiers must survive every fast path."""

from datetime import date, datetime
from pathlib import Path
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.agent_core.planning import RequestPlanner
from app.query_scope import ScopeResolver
from app.tests.test_price_group_sales import question as channel_question
from app.tests.test_customer_channel_aggregation import channels
from app.tests.test_sales_aggregation import facts
from app.tests.test_sales_detail_chat import build
from app.sku_references import extract_sku_references, restore_sku_references


@pytest.mark.parametrize("restriction", [
    "所有货主HJXH", "HJXH全部货主", "所有仓库天津仓", "天津仓全部仓库",
    "所有SKU，SKU `AbC123`", "SKU `AbC123`，全部SKU",
    "所有商品，SKU `AbC123`", "全部货品，SKU `AbC123`",
])
def test_conflicting_all_and_specific_scopes_are_not_silently_narrowed(restriction):
    assert ScopeResolver().resolve("昨天" + restriction + "实际销售出库")[2]


@pytest.mark.parametrize("question", [
    "昨天所有SKU，SKU `AbC123`实际销售出库明细",
    "查询昨天所有SKU，SKU `AbC123`实际销售出库，按仓库，计算数量占比",
    "昨天所有货主HJXH实际销售出库数量，按仓库汇总",
    "看下上周所有SKU，SKU `AbC123`线下渠道出库数量，单量，箱数",
    "查询最近7个完整自然日生产日期的采购入库明细，不包含今天完成入库的数据，所有SKU，SKU `AbC123`，返回仓库、入库单、SKU、生产日期、实收入库数量和实际入库时间。",
    channel_question(scope="所有仓库天津仓成品"),
    channel_question(scope="所有货主HJXH成品"),
])
def test_conflicts_cannot_reach_single_scope_execution(question):
    assert RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(question) is None


@pytest.mark.parametrize("code", [
    "AbC-JYCK", "AbC-HHCK", "AbC-JYCK-HHCK", "AbC-2C", "AbC-TOP20", "AbC-2026-01-02", "HJXH-TJWWC-777",
])
def test_channel_boxes_treat_identifiers_as_data_not_operators(code):
    request = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(
        f"看下上周SKU `{code}`线下渠道出库数量，单量，箱数"
    )
    assert request is not None and request.route == "sales_outbound_boxes_channel"
    assert request.scope.sku_codes == (code,)
    assert request.scope.owner == "ALL" and not request.scope.warehouse_codes
    assert request.scope.channel == "线下"
    assert request.sales_aggregation.order_types == ()
    assert request.sales_aggregation.fulfillment_type is None
    assert (request.start, request.end) == (date(2026, 8, 31), date(2026, 9, 7))


@pytest.mark.parametrize("scope", ["所有SKU", "所有仓库", "全部货主"])
def test_unrestricted_scope_remains_executable(scope):
    request = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(
        f"近14天{scope}实际销售出库明细"
    )
    assert request is not None and request.route == "sales_outbound_details"
    assert request.scope.owner == "ALL" and not request.scope.sku_codes and not request.scope.warehouse_codes


def test_all_markers_inside_sku_do_not_create_a_scope_conflict():
    scope, _, unsupported = ScopeResolver().resolve("昨天SKU `ALL-HJXH-TJWWC-777`实际销售出库")
    assert not unsupported
    assert scope.owner == "ALL" and not scope.warehouse_codes


def test_explicit_order_type_remains_separate_from_sku_identifier():
    request = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(
        "看下上周JYCK SKU `AbC-HHCK`线下渠道出库数量，单量，箱数"
    )
    assert request is not None and request.route == "sales_outbound_boxes_channel"
    assert request.scope.sku_codes == ("AbC-HHCK",)
    assert request.sales_aggregation.order_types == ("JYCK",)


def test_box_contract_does_not_drop_all_scope_conflict(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("app.agent_core.planning.active_snapshot", lambda _: (root, None))
    text = (
        "查询昨天 JYCK 成品出库，所有SKU，SKU `AbC123`，返回原始实发数量、箱规和实发箱数。"
        "箱数按汇总后的数量除以箱规，四舍五入且不保留小数。"
    )
    assert RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(text) is None


@pytest.mark.parametrize("scope,codes", [("所有SKU", ()), ("全部SKU", ()), ("SKU `AbC123`", ("AbC123",))])
def test_receipt_projection_does_not_rewrite_scope_declarations(scope, codes):
    text = (
        f"查询最近7个完整自然日生产日期的采购入库明细，不包含今天完成入库的数据，{scope}，"
        "返回仓库、入库单、SKU、生产日期、实收入库数量和实际入库时间。"
    )
    request = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12)).classify(text)
    assert request is not None and request.route == "purchase_received_detail"
    assert request.scope.sku_codes == codes and request.receipt_plan.sku_codes == codes


def test_restoration_is_a_lossless_scope_roundtrip():
    codes = ("A", "MiXeD-2C", "SKU-2026-01-02")
    restored = restore_sku_references("昨天实际销售出库", codes)
    actual, semantic = extract_sku_references(restored)
    assert actual == codes and "2026" not in semantic
    assert restore_sku_references("昨天实际销售出库", ()) == "昨天实际销售出库"
    with pytest.raises(ValueError):
        restore_sku_references("昨天实际销售出库", ("bad`code",))


@pytest.mark.parametrize("code", ["AbC-HHCK", "AbC-JYCK-HHCK", "AbC-2C", "AbC-TOP20", "AbC-2026-01-02"])
def test_public_channel_boxes_match_source_subset_not_identifier_tokens(channels, tmp_path, code):
    channels.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET sku_code=? WHERE sku_code='A'", (code,))
    channels.execute("UPDATE ywzt_ods.master_sku SET sku_code=? WHERE sku_code='A'", (code,))
    channels.execute("CREATE TABLE ywzt_ods.master_sku_unit (sku_code TEXT,unit TEXT,ea_convert_quantity INTEGER)")
    channels.execute("INSERT INTO ywzt_ods.master_sku_unit VALUES (?,'40',10)", (code,))
    app, query = build(channels, tmp_path)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()

        def submit(question, key):
            response = client.post("/v1/chat", json={
                "question": question, "conversation_id": session["conversation_id"],
            }, headers={"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": key})
            assert response.status_code == 200, response.json()
            return response.json()

        question = f"看下昨天SKU `{code}`线上渠道出库数量，单量，箱数"
        result = submit(question, "opaque-sku-public-001")
        assert result["result_state"] == "live_success"
        assert result["clarification"]["required"] is False
        table = result["business_table"]
        assert {row["sku_code"] for row in table["rows"]} == {code}
        assert {row["customer_channel"] for row in table["rows"]} == {"线上"}
        assert {item["label"]: Decimal(item["value"]) for item in table["summary"]} == {
            "实际销售出库": Decimal("3.75"), "出库单数": Decimal(3),
        }
        calls = query.calls
        assert submit(question, "opaque-sku-public-001") == result
        assert query.calls == calls
        following = submit("看下昨天线上渠道出库数量，单量，箱数", "opaque-sku-public-002")
        assert {row["sku_code"] for row in following["business_table"]["rows"]} == {code, "B"}
        assert following["business_table"]["summary"][0]["value"] == "8"
