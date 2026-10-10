"""Synthetic channel facts and regressions for the reported request."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlglot

from app.tests.test_sales_aggregation import facts
from app.agent_core.planning import RequestPlanner
from app.agent_core.price_group_sales import ENUM_PARENT, PriceGroupSalesHandler, compile_price_group_sales
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.query_scope import QueryScope, apply_scope
from app.chat_api import _public_business_table


def question(period="2026-09-09 全天", scope="HJXH 货主产成品"):
    return (f"查询 {period} {scope}销售出库，按‘渠道细分 + 2C/2B’汇总，输出出库单数、实发数量、SKU数。\n"
            "口径：\n- 出库状态：700、900、701、901\n- 产成品：master_sku.sku_group = '50'\n"
            "- 2C：terms_of_shipment = '20'\n- 渠道细分取 master_store.price_group，关联枚举父级："
            + ENUM_PARENT + "\n- 排除逻辑删除数据")


@pytest.fixture(autouse=True)
def scope_rules(monkeypatch):
    monkeypatch.setattr("app.query_scope.active_snapshot", lambda root: (Path(__file__).resolve().parents[2], None))


@pytest.fixture
def channels(facts):
    facts.execute("ALTER TABLE ywzt_ods.scm_sales_shipment_header ADD COLUMN store_code TEXT")
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code='C1'")
    facts.execute("CREATE TABLE ywzt_ods.master_store(company_code TEXT,code TEXT,price_group TEXT,delete_flag INT,updated TEXT)")
    facts.executemany("INSERT INTO ywzt_ods.master_store VALUES (?,?,?,0,'2026-09-10 08:00:00')", [
        ('HJXH','C1','10'),('HJXH','C1','10'),('B','C1','202')])
    facts.execute("CREATE TABLE ywzt_ods.sys_enum(CODE_ TEXT,NAME_ TEXT,PARENT_CODE_ TEXT,DELETED_ INT)")
    facts.executemany("INSERT INTO ywzt_ods.sys_enum VALUES (?,?,?,0)", [
        ('10','测试渠道甲',ENUM_PARENT),('10','测试渠道甲',ENUM_PARENT),('202','测试渠道乙',ENUM_PARENT)])
    return facts


def read(db, scope=QueryScope(owner="HJXH", finished_goods=True)):
    sql = apply_scope(compile_price_group_sales(date(2026,9,9),date(2026,9,10)),scope,legacy_owner="HJXH")
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read="doris",write="sqlite")[0])]


def answer(rows):
    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            return rows
    return PriceGroupSalesHandler(Execution(),timezone_name="Asia/Shanghai").run(
        SemanticRequest("sales_price_group",date(2026,9,9),date(2026,9,10),scope=QueryScope(owner="HJXH",finished_goods=True)))


@pytest.mark.parametrize("period,start,end", [
    ("2026-09-09 全天",date(2026,9,9),date(2026,9,10)),
    ("最近7天",date(2026,9,7),date(2026,9,14)),
    ("最近14天",date(2026,8,31),date(2026,9,14)),
    ("2026-08-28至2026-09-10",date(2026,8,28),date(2026,9,11)),
])
def test_explicit_mapping_has_complete_scope_and_window(period,start,end):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,14,12,tzinfo=ZoneInfo("Asia/Shanghai")))
    request=planner.classify(question(period))
    assert request is not None and request.route=="sales_price_group"
    assert (request.start,request.end)==(start,end)
    assert request.scope.owner=="HJXH" and request.scope.finished_goods


def test_unknown_conditions_never_disappear():
    planner=RequestPlanner()
    for extra in ("，排除客户甲", "，仅限北京未知仓", "，数量>0", "，单位为箱"):
        request=planner.classify(question()+extra)
        assert request is None or request.route!="sales_price_group"
    assert planner.classify(question().replace(ENUM_PARENT,"unknown_enum")).price_group_parent == "unknown_enum"
    assert planner.classify(question().replace("master_store.price_group","master_store.platform_code")) is None


def test_business_spacing_and_document_count_wording_keep_price_group_route():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(
        "查询2026-09-11全天 HJXH 产成品销售出库，按渠道细分 + 2C/2B汇总，"
        "输出出库单笔数、实发数量、SKU数。渠道细分取 master_store.price_group，"
        "关联枚举父级：" + ENUM_PARENT + "；排除逻辑删除数据"
    )
    assert request is not None and request.route == "sales_price_group"
    assert request.price_group_parent == ENUM_PARENT
    assert request.start == date(2026, 9, 11) and request.end == date(2026, 9, 12)


def test_plain_business_channel_wording_uses_reviewed_default_mapping():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(
        "查询2026-09-11全天 HJXH 生产产品成品销售出库，按渠道细分和2C/2B汇总，"
        "输出出库单数、实发数量、SKU数；排除逻辑删除数据"
    )
    assert request is not None and request.route == "sales_price_group"
    assert request.price_group_parent == ENUM_PARENT
    assert request.start == date(2026, 9, 11) and request.end == date(2026, 9, 12)


def test_compact_business_rules_keep_deterministic_route():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(
        "查询2026-09-11全天HJXH生产产品成品销售出库，按渠道细分和2C/2B汇总，"
        "输出出库单数、实发数量、SKU数。口径：出库状态700、900、701、901；"
        "产品组50；2C terms_of_shipment=20；渠道细分按客户主数据分类；排除逻辑删除数据"
    )
    assert request is not None and request.route == "sales_price_group"
    assert request.price_group_parent == ENUM_PARENT


@pytest.mark.parametrize("types,expected_quantity,expected_documents", [
    (("JYCK",), Decimal("8"), 1),
    (("JYCK", "HHCK"), Decimal("8"), 2),
    (("HHCK", "SO07"), Decimal("10"), 2),
])
def test_channel_handler_preserves_explicit_type_set(channels, types, expected_quantity, expected_documents):
    request = RequestPlanner().classify(question(scope="全部货主" + "和".join(types)))
    assert request is not None and request.route == "sales_price_group"
    assert request.sales_aggregation.order_types == types
    from dataclasses import replace
    request = replace(request, finished_goods_only=False, scope=QueryScope())
    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            return [dict(r) for r in channels.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    answer = PriceGroupSalesHandler(Execution(), timezone_name="Asia/Shanghai").run(request)
    rows = answer.business_table["rows"]
    assert sum(Decimal(row["quantity"]) for row in rows) == expected_quantity
    assert sum(int(row["outbound_count"]) for row in rows) == expected_documents


def test_scope_resets_and_retains_named_warehouse():
    planner=RequestPlanner()
    first=planner.classify(question(scope="HJXH 货主产成品 长沙黄兴"))
    second=planner.classify(question(scope="全部货主产成品"))
    assert first.scope.warehouse_codes==("10114790",)
    assert second.scope.owner=="ALL" and second.scope.warehouse_codes==()


@pytest.mark.parametrize("separator", ["\n", ";", "；"])
@pytest.mark.parametrize("colon", ["：", ":", ""])
def test_rule_layout_does_not_force_model_fallback(separator, colon):
    text = question().replace("\n", separator).replace("：", colon)
    # The narrative label still needs punctuation, unlike the fixed rule labels.
    if not colon:
        text = text.replace("口径", "口径：")
    request = RequestPlanner().classify(text)
    assert request is not None and request.route == "sales_price_group"
    assert request.scope.owner == "HJXH" and request.scope.finished_goods
    assert request.finished_goods_only
    assert (request.start, request.end) == (date(2026, 9, 9), date(2026, 9, 10))


@pytest.mark.parametrize("states", ["901、701、900、700", "700,900,701,901", "700，900，701，901"])
def test_status_order_and_list_separator_do_not_change_population(states):
    request = RequestPlanner().classify(question().replace("700、900、701、901", states))
    assert request is not None and request.route == "sales_price_group"


@pytest.mark.parametrize("states", ["700、900", "700、900、701、901、999", "700、900、701、9010", "700/900/701/901/999"])
def test_different_status_population_never_uses_default_contract(states):
    request = RequestPlanner().classify(question().replace("700、900、701、901", states))
    assert request is None


@pytest.mark.parametrize("quote", ["'", '"'])
def test_rule_only_finished_goods_filter_is_not_lost(quote):
    text = question(scope="HJXH货主").replace("'50'", quote + "50" + quote)
    request = RequestPlanner().classify(text)
    assert request is not None and request.route == "sales_price_group"
    assert request.finished_goods_only and request.scope.finished_goods


def test_additional_predicate_after_inline_rules_cannot_disappear():
    text = question().replace("\n", "；").replace("出库状态：", "出库状态") + "；排除客户甲"
    request = RequestPlanner().classify(text)
    assert request is None


def test_enum_identifier_must_match_in_full():
    request = RequestPlanner().classify(question().replace(ENUM_PARENT, ENUM_PARENT + "123"))
    assert request.price_group_parent == ENUM_PARENT + "123"


def test_different_enum_parent_is_not_an_alias():
    transposed = "2c9c0da1948322ee01948bac2924000a"
    request = RequestPlanner().classify(question().replace(ENUM_PARENT, transposed))
    assert request is not None and request.route == "sales_price_group"
    assert request.price_group_parent == transposed


def test_product_group_wording_keeps_deterministic_route():
    request = RequestPlanner().classify(question().replace("产成品：", "产品组："))
    assert request is not None and request.route == "sales_price_group"


def test_screenshot_wording_keeps_deterministic_route():
    text = question().replace("产成品", "产品").replace("产品：", "产品组：")
    text = text.replace("渠道细分取", "渠道细分按").replace("出库单数", "出库单单数")
    request = RequestPlanner().classify(text)
    assert request is not None and request.route == "sales_price_group"


def test_deduplicated_mapping_zero_shipments_and_finished_goods(channels):
    rows=read(channels)
    assert sum(Decimal(str(r["quantity"])) for r in rows)==Decimal("3.75")
    assert sum(r["outbound_count"] for r in rows)==3
    assert {r["fulfillment_type"] for r in rows}=={"2C","2B","未配置"}
    assert all(r["total_sku_count"]==r["sku_count"]==1 for r in rows)
    assert all(r["conflict_count"]==0 for r in rows)
    table=_public_business_table(answer(rows).business_table,live=True)
    assert table is not None and len(table["rows"])==3
    assert {c["key"] for c in table["columns"]}=={"channel_code","channel_name","fulfillment_type","quantity","outbound_count","sku_count"}


def test_all_owner_scope_never_merges_same_document_code(channels):
    rows=read(channels,QueryScope(finished_goods=True))
    assert sum(Decimal(str(r["quantity"])) for r in rows)==Decimal("13.75")
    assert sum(r["outbound_count"] for r in rows)==4
    answer(rows)


@pytest.mark.parametrize("source",["customer","enum"])
def test_conflicting_mapping_cannot_fabricate_or_multiply_result(channels,source):
    if source=="customer":
        channels.execute("INSERT INTO ywzt_ods.master_store VALUES ('HJXH','C1','202',0,'2026-09-10 08:00:00')")
    else:
        channels.execute("INSERT INTO ywzt_ods.sys_enum VALUES ('10','另一个名称',?,0)",(ENUM_PARENT,))
    rows=read(channels)
    assert sum(Decimal(str(r["quantity"])) for r in rows)==Decimal("3.75")
    with pytest.raises(SemanticRuntimeError):
        answer(rows)


@pytest.mark.parametrize("source",["customer","enum"])
def test_deleted_or_missing_mapping_preserves_shipments(channels,source):
    channels.execute("UPDATE ywzt_ods.master_store SET delete_flag=1" if source=="customer" else "UPDATE ywzt_ods.sys_enum SET DELETED_=1")
    rows=read(channels)
    assert sum(r["outbound_count"] for r in rows)==3
    assert all(r["channel_name"]=="未匹配渠道" for r in rows)
    answer(rows)


@pytest.mark.parametrize("key,value",[("quantity",999),("outbound_count",999),("sku_count",0),
    ("total_sku_count",0),("group_count",500),("conflict_count",1),("null_quantity_count",1),
    ("negative_quantity_count",1),("blank_key_count",1)])
def test_bad_or_partial_evidence_never_becomes_a_table(channels,key,value):
    rows=read(channels)
    rows[0][key]=value
    with pytest.raises(SemanticRuntimeError):
        answer(rows)


def test_empty_result_and_row_cap_are_distinct():
    assert _public_business_table(answer([]).business_table,live=True)["rows"]==[]
    with pytest.raises(SemanticRuntimeError) as raised:
        answer([{}]*501)
    assert raised.value.reason=="result_truncated"
