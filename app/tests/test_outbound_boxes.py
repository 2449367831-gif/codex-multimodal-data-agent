from datetime import date, datetime
from pathlib import Path

import pytest
import sqlglot

from app.tests.test_customer_channel_aggregation import channels
from app.tests.test_sales_aggregation import facts
from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRuntimeError
from app.agent_core.sales_pages import retrieve_sales_groups, aggregate_page_sql
from app.agent_core.sales_aggregation import compile_sales_aggregation, SalesAggregation


QUESTION = "查询昨天 JYCK 成品出库，返回原始实发数量、箱规和实发箱数。箱数按汇总后的数量除以箱规，四舍五入且不保留小数。"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def candidate(monkeypatch):
    monkeypatch.setattr("app.agent_core.planning.active_snapshot",lambda _: (ROOT,None))


def client_for(db):
    class Client:
        def query(self, *, sql, purpose):
            rows = [dict(r) for r in db.execute(sqlglot.transpile(sql,read="doris",write="sqlite")[0])]
            return {"status":"succeeded","execution":{"executor_name":"doris","is_mock":False,"rows":rows,"row_count":len(rows),"truncated":False}}
    return Client()


def test_complete_question_routes_to_customer_channels_and_resets_scope():
    planner = RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    plan = planner.classify(QUESTION)
    assert plan and plan.route == "sales_outbound_boxes"
    assert plan.sales_aggregation.dimensions == ("ship_date","warehouse","sku","customer_channel")
    assert plan.start == date(2026,9,9) and plan.end == date(2026,9,10)
    assert plan.scope.owner == "ALL" and plan.scope.finished_goods and not plan.scope.finished_goods_exceptions
    assert planner.classify(QUESTION.replace("昨天", "昨天 HJXH 货主")).scope.owner == "HJXH"
    assert planner.classify(QUESTION).scope.owner == "ALL"


def test_short_channel_quantity_count_box_question_uses_all_actual_sales_types():
    plan = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12)).classify(
        "看下上周线下渠道出库数量，单量，箱数"
    )
    assert plan is not None and plan.route == "sales_outbound_boxes_channel"
    assert plan.start == date(2026, 9, 7) and plan.end == date(2026, 9, 14)
    assert plan.sales_aggregation.channel_filter == "线下"
    assert plan.sales_aggregation.order_types == ()
    assert plan.finished_goods_only is False


def test_channel_boxes_keep_non_finished_goods_and_missing_specs(channels):
    channels.execute("CREATE TABLE ywzt_ods.master_sku_unit (sku_code TEXT,unit TEXT,ea_convert_quantity INTEGER)")
    channels.execute("INSERT INTO ywzt_ods.master_sku_unit VALUES ('A','40',10)")
    runtime = SemanticQueryRuntime(query_client=client_for(channels), clock=lambda: datetime(2026, 9, 10, 12))
    result = runtime.run_if_supported("看下昨天线上渠道出库数量，单量，箱数")
    table = result["business_table"]
    assert {row["sku_code"] for row in table["rows"]} == {"A", "B"}
    assert all(row["customer_channel"] == "线上" for row in table["rows"])
    from decimal import Decimal
    assert {item["label"]: Decimal(item["value"]) for item in table["summary"]} == {
        "实际销售出库": Decimal(8), "出库单数": Decimal(3),
    }
    assert next(row for row in table["rows"] if row["sku_code"] == "B")["box_quantity"] is None
    from app.chat_api import _public_business_table
    public = _public_business_table(table, live=True)
    assert public is not None and "成品" not in public["title"]


@pytest.mark.parametrize("suffix", ["按仓库汇总", "排除仓库甲", "向下取整", "按SKU排名", "2C"])
def test_channel_shortcut_does_not_discard_extra_operations(suffix):
    from app.agent_core.box_planning import filtered_outbound_box_request
    from app.query_scope import ScopeResolver
    assert filtered_outbound_box_request(
        "上周线下渠道出库数量、单量、箱数" + suffix,
        interval=(date(2026, 9, 7), date(2026, 9, 14)),
        resolver=ScopeResolver(), contract_root=ROOT,
    ) is None


def test_contract_preserves_confirmed_channel_without_reopening_the_choice():
    import yaml

    contract = yaml.safe_load((ROOT / "metrics/sales.outbound_boxes.yaml").read_text(encoding="utf-8"))
    assert "customer_channel" in contract["grain"]
    assert "master_store.type" in contract["grain"]
    assert "不重复提问" in contract["clarification"]["question"]
    assert "2C" not in contract["clarification"]["question"]
    assert "does not approve customer identity joins" in contract["filters"]["channel_decision"]


@pytest.mark.parametrize("selection", [
    "线上／线下／其他（客户主数据分类）",
    "线上/线下/其他（客户主数据分类）",
    "按日期、仓库、SKU、渠道汇总，线上／线下／其他",
])
def test_confirmed_customer_channel_choice_keeps_the_same_plan(selection):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    expected = planner.classify(QUESTION)
    selected = planner.classify(QUESTION + selection)
    assert selected is not None
    assert selected == expected


@pytest.mark.parametrize("changed", [QUESTION+"仅限客户甲",QUESTION+"按2C/2B划分",QUESTION.replace("四舍五入", "向下取整"),QUESTION.replace("汇总后的数量", "逐行数量"),QUESTION.replace("JYCK","HHCK"),QUESTION+"排除云仓",QUESTION.replace("昨天",""),QUESTION.replace("成品","所有商品")])
def test_unreviewed_filters_and_rounding_not_silently_lost(changed):
    assert RequestPlanner(clock=lambda:datetime(2026,9,10,12)).classify(changed) is None


def test_box_definition_does_not_silently_reduce_a_type_union_to_jyck():
    assert RequestPlanner().classify(QUESTION.replace("JYCK", "JYCK和HHCK")) is None


def test_runtime_preserves_raw_ea_and_missing_box_cells(channels):
    channels.execute("CREATE TABLE ywzt_ods.master_sku_unit (sku_code TEXT,unit TEXT,ea_convert_quantity INTEGER)")
    channels.execute("INSERT INTO ywzt_ods.master_sku_unit VALUES ('A','40',10)")
    runtime = SemanticQueryRuntime(query_client=client_for(channels),clock=lambda:datetime(2026,9,10,12))
    result = runtime.run_if_supported(QUESTION)
    assert result and result["result_state"] == "live_success"
    table = result["business_table"]
    assert table["complete_reconciled"] is True and len(table["rows"]) == 1
    row = table["rows"][0]
    assert row["quantity"] == "3.75" and row["box_quantity"] == "0" and row["customer_channel"] == "线上"
    channels.execute("DELETE FROM ywzt_ods.master_sku_unit")
    missing = runtime.run_if_supported(QUESTION)["business_table"]["rows"][0]
    assert missing["quantity"] == "3.75" and missing["box_quantity"] is None and missing["box_state"] == "缺少箱规"
    from app.chat_api import _public_business_table
    public = _public_business_table(runtime.run_if_supported(QUESTION)["business_table"],live=True)
    assert public is not None and len(public["columns"]) == 11
    assert public["rows"][0]["box_spec"] is None and public["rows"][0]["box_quantity"] is None
    assert any("空值不是零箱" in note for note in public["notes"])


def test_runtime_conflicting_customer_types_block_instead_of_wrong_box_rounding(channels):
    channels.execute("INSERT INTO ywzt_ods.master_store VALUES ('HJXH','C1','Z001')")
    with pytest.raises(SemanticRuntimeError):
        SemanticQueryRuntime(query_client=client_for(channels),clock=lambda:datetime(2026,9,10,12)).run_if_supported(QUESTION)


def test_customer_channel_is_the_rounding_grain_not_fulfillment_or_detail(channels):
    from decimal import Decimal

    for table in ("scm_sales_shipment_header", "scm_sales_shipment_detail", "scm_sales_order_header", "master_store"):
        channels.execute(f"DELETE FROM ywzt_ods.{table}")
    channels.execute("CREATE TABLE ywzt_ods.master_sku_unit (sku_code TEXT,unit TEXT,ea_convert_quantity INTEGER)")
    channels.execute("INSERT INTO ywzt_ods.master_sku_unit VALUES ('A','40',10)")
    channels.executemany("INSERT INTO ywzt_ods.master_store VALUES ('HJXH',?,?)", [
        ("ONLINE", "Z004"), ("OFFLINE", "Z001"), ("OTHER", "UNKNOWN"),
    ])
    # Online spans both fulfillment modes; each detail is below half a box.
    for index, (customer, fulfillment) in enumerate([
        ("ONLINE", "20"), ("ONLINE", "30"), ("OFFLINE", "20"), ("OTHER", "30"),
    ]):
        document = f"ROUND-{index}"
        channels.execute(
            "INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES "
            "('HJXH',?,'W1','2026-09-09 12:00:00',700,'JYCK',?,?,0,'false',?)",
            (document, document, fulfillment, customer),
        )
        channels.execute("INSERT INTO ywzt_ods.scm_sales_order_header VALUES ('HJXH',?,0,'false')", (document,))
        channels.execute("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES ('HJXH',?,'A',4,0,'false')", (document,))

    runtime = SemanticQueryRuntime(query_client=client_for(channels),clock=lambda:datetime(2026,9,10,12))
    table = runtime.run_if_supported(QUESTION)["business_table"]
    rows = table["rows"]
    assert len(rows) == 3
    assert {row["customer_channel"]: (row["quantity"], row["box_quantity"]) for row in rows} == {
        "线上": ("8", "1"), "线下": ("4", "0"), "其他": ("4", "0"),
    }
    assert sum(Decimal(row["quantity"]) for row in rows) == Decimal(16)
    assert all(row["cargo_owner"] == "HJXH" and row["warehouse_code"] == "W1" and row["sku_code"] == "A" for row in rows)
    assert all(row["ship_date"] == "2026-09-09" for row in rows)


def test_independent_reference_executes_and_matches_fixture_contract(channels):
    import runpy
    reference=runpy.run_path(str(ROOT/"tests/acceptance/check_outbound_boxes_live.py"))
    channels.execute("CREATE TABLE ywzt_ods.master_sku_unit (sku_code TEXT,unit TEXT,ea_convert_quantity INTEGER)")
    channels.execute("INSERT INTO ywzt_ods.master_sku_unit VALUES ('A','40',10)")
    client=client_for(channels)
    runtime=SemanticQueryRuntime(query_client=client,clock=lambda:datetime(2026,9,10,12))
    table=runtime.run_if_supported(QUESTION)["business_table"]
    sql=reference["reference_sql"](date(2026,9,9),date(2026,9,10))
    rows=client.query(sql=aggregate_page_sql(sql,0,500),purpose="independent fixture")["execution"]["rows"]
    assert all(reference["independently_compare"](table,rows).values())
    from app.chat_api import _public_business_table
    assert all(reference["independently_compare"](_public_business_table(table,live=True),rows).values())


def test_complete_pages_return_all_groups_and_detect_changes(channels):
    sql = compile_sales_aggregation(SalesAggregation(("warehouse","sku","customer_channel")),start=date(2026,9,9),end=date(2026,9,10))
    calls = []
    def query(sql, *, purpose):
        calls.append(sql)
        return client_for(channels).query(sql=sql,purpose=purpose)["execution"]["rows"]
    rows = retrieve_sales_groups(sql,query,page_size=1)
    assert len(rows) == 4 and len(calls) == 8
    assert [r["result_ordinal"] for r in rows] == list(range(1,5))
    with pytest.raises(SemanticRuntimeError) as capacity:
        retrieve_sales_groups(sql,query,page_size=1,max_rows=2)
    assert capacity.value.reason == "result_truncated"
    calls.clear()
    def changing(sql, *, purpose):
        batch = query(sql,purpose=purpose)
        if len(calls) > 4:
            batch[0]["quantity"] += 1
        return batch
    with pytest.raises(SemanticRuntimeError) as changed:
        retrieve_sales_groups(sql,changing,page_size=1)
    assert changed.value.reason == "sales_page_invalid"


@pytest.mark.parametrize("failure", ["missing", "ordinal", "count", "bytes"])
def test_invalid_page_never_produces_partial_result(failure):
    def query(sql, *, purpose):
        row = {"result_ordinal":2 if failure == "ordinal" else 1,"group_count":2 if failure == "missing" else 1,"quantity":"1"}
        if failure == "count": row["group_count"] = "1.5"
        return [row]
    with pytest.raises(SemanticRuntimeError):
        retrieve_sales_groups("SELECT 1 ORDER BY 1 LIMIT 501",query,max_bytes=1 if failure == "bytes" else 64000)
