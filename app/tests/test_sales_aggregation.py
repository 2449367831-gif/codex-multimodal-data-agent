"""Execute the compiler against synthetic facts before any live reconciliation."""

from datetime import date, datetime
from decimal import Decimal
from itertools import combinations
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlglot

from app.agent_core.sales_aggregation import DIMENSION_KEYS, SalesAggregation, compile_sales_aggregation
from app.agent_core.handlers.sales_aggregate import SalesAggregateHandler
from app.agent_core.planning import RequestPlanner
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.query_scope import QueryScope, apply_scope
from app.chat_api import PUBLIC_TABLE_SCHEMAS, _public_business_table
from app.agent_core.runtime import SemanticQueryRuntime


@pytest.fixture(autouse=True)
def candidate_scope_rules(monkeypatch):
    monkeypatch.setattr("app.query_scope.active_snapshot", lambda root: (Path(__file__).resolve().parents[2], None))


@pytest.fixture
def facts():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.executescript("""
    CREATE TABLE ywzt_ods.scm_sales_shipment_header (
      company_code TEXT, outbound_code TEXT, facility_code TEXT, ship_at TEXT,
      status INTEGER, ref_order_type TEXT, ref_order_code TEXT,
      terms_of_shipment TEXT, delete_flag INTEGER, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.scm_sales_shipment_detail (
      company_code TEXT, outbound_code TEXT, sku_code TEXT, ship_qty NUMERIC,
      delete_flag INTEGER, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.scm_sales_order_header (
      company_code TEXT, sales_order_code TEXT, deleted_flag INTEGER, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.master_wms (
      cargo_owner_id TEXT, wms_code TEXT, wms_desc TEXT, status TEXT);
    CREATE TABLE ywzt_ods.master_sku (sku_code TEXT, sku_name TEXT, sku_group TEXT);
    """)
    headers = [
        ("HJXH", "O1", "W1", "2026-09-09 00:00:00", 700, "JYCK", "S1", "20", 0, "false"),
        ("HJXH", "O1", "W1", "2026-09-09 00:00:00", 700, "JYCK", "S1", "20", 0, "false"),
        ("HJXH", "O2", "W1", "2026-09-09 23:59:59", 701, "HHCK", "S2", "30", 0, "false"),
        ("HJXH", "O3", "W2", "2026-09-09 12:00:00", 901, "BFCK", "S3", None, 0, "false"),
        ("B", "O1", "W1", "2026-09-09 12:00:00", 900, "SO07", "S1", "20", 0, "false"),
        ("HJXH", "MIDNIGHT", "W1", "2026-09-10 00:00:00", 700, "JYCK", "S4", "20", 0, "false"),
        ("HJXH", "OLD", "W1", "2026-09-08 23:59:59", 700, "QTCK", "S5", "20", 0, "false"),
        ("HJXH", "DELETED", "W1", "2026-09-09 12:00:00", 700, "JYCK", "S6", "20", 1, "false"),
    ]
    db.executemany("INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES (?,?,?,?,?,?,?,?,?,?)", headers)
    db.executemany("INSERT INTO ywzt_ods.scm_sales_order_header VALUES (?,?,0,'false')",
                   [(h[0], h[6]) for h in headers] + [("HJXH", "S1")])
    details = [
        ("HJXH", "O1", "A", "1.25", 0, "false"),
        ("HJXH", "O1", "A", "2.50", 0, "false"),
        ("HJXH", "O1", "B", "4.25", 0, "false"),
        ("HJXH", "O2", "A", "0", 0, "false"),
        ("HJXH", "O3", "A", "0", 0, "false"),
        ("B", "O1", "A", "10", 0, "false"),
        ("HJXH", "MIDNIGHT", "A", "100", 0, "false"),
        ("HJXH", "OLD", "A", "1000", 0, "false"),
        ("HJXH", "DELETED", "A", "10000", 0, "false"),
        ("HJXH", "O1", "A", "100000", 1, "false"),
    ]
    db.executemany("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES (?,?,?,?,?,?)", details)
    db.executemany("INSERT INTO ywzt_ods.master_wms VALUES (?,?,?, '0')", [
        ("HJXH", "W1", "测试同名仓"), ("HJXH", "W1", "测试同名仓"),
        ("HJXH", "W2", "测试二仓"), ("B", "W1", "测试同名仓"),
    ])
    db.executemany("INSERT INTO ywzt_ods.master_sku VALUES (?,?,?)", [("A", "测试成品", "50"), ("B", "测试辅料", "60")])
    yield db
    db.close()


def execute(db, plan, scope=QueryScope()):
    sql = compile_sales_aggregation(plan, start=date(2026, 9, 9), end=date(2026, 9, 10))
    scoped = apply_scope(sql, scope, legacy_owner="HJXH")
    sqlite_sql = sqlglot.transpile(scoped, read="doris", write="sqlite")[0]
    return [dict(row) for row in db.execute(sqlite_sql)]


def test_warehouse_quantity_and_document_counts_do_not_multiply(facts):
    rows = execute(facts, SalesAggregation(("warehouse",)), QueryScope(owner="HJXH"))
    assert [(r["warehouse_code"], r["quantity"], r["outbound_count"]) for r in rows] == [("W1", 8, 2), ("W2", 0, 1)]
    assert all(r["total_quantity"] == 8 and r["total_outbound_count"] == 3 for r in rows)


def test_sku_count_is_independent_from_warehouse_quantity_and_document_counts(facts):
    plan = SalesAggregation(("warehouse",), include_sku_count=True)
    rows = execute(facts, plan, QueryScope(owner="HJXH"))
    assert [(r["warehouse_code"], r["sku_count"]) for r in rows] == [("W1", 2), ("W2", 1)]
    assert all(r["total_sku_count"] == 2 for r in rows)

    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            return execute(facts, plan, QueryScope(owner="HJXH"))

    request = SemanticRequest("sales_aggregate", date(2026, 9, 9), date(2026, 9, 10), sales_aggregation=plan,
                              scope=QueryScope(owner="HJXH"))
    table = SalesAggregateHandler(Execution(), timezone_name="Asia/Shanghai").run(request).business_table
    assert [column["key"] for column in table["columns"]][-1] == "sku_count"
    assert table["summary"][-1] == {"label": "SKU数", "value": "2", "unit": "个"}
    public = _public_business_table(table, live=True)
    assert public is not None
    assert [column["key"] for column in public["columns"]][-1] == "sku_count"
    assert public["summary"][-1] == {"label": "SKU数", "value": "2", "unit": "个"}


def test_sku_count_type_filter_and_freshness_notes_remain_displayable(facts):
    plan = SalesAggregation(("warehouse",), ref_order_type="JYCK", include_sku_count=True)

    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            return execute(facts, plan, QueryScope(owner="HJXH"))

    request = SemanticRequest("sales_aggregate", date(2026, 9, 9), date(2026, 9, 10), sales_aggregation=plan,
                              scope=QueryScope(owner="HJXH"))
    raw = SalesAggregateHandler(Execution(), timezone_name="Asia/Shanghai").run(request).business_table
    table = _public_business_table(raw, live=True)
    assert table is not None and len(table["notes"]) <= 3
    notes = "".join(table["notes"])
    assert all(marker in notes for marker in ("SKU数", "JYCK", "零数量", "数据同步时间"))


@pytest.mark.parametrize("table_id", ["sales_aggregate", "sales_analysis"])
def test_all_sales_dimensions_and_measures_have_public_descriptors(table_id):
    required = {key for keys in DIMENSION_KEYS.values() for key in keys}
    required.update({"warehouse_name", "quantity", "outbound_count", "sku_count"})
    assert required <= {column["key"] for column in PUBLIC_TABLE_SCHEMAS[table_id]["columns"]}


@pytest.mark.parametrize("table_id", ["sales_aggregate", "sales_analysis"])
def test_channel_group_is_accepted_by_public_sales_table(table_id):
    table = {
        "format": "table",
        "table_id": table_id,
        "columns": [
            {"key": "cargo_owner", "kind": "text"},
            {"key": "warehouse_code", "kind": "text"},
            {"key": "sku_code", "kind": "text"},
            {"key": "customer_channel", "kind": "text"},
            {"key": "quantity", "kind": "number", "unit": "EA"},
            {"key": "outbound_count", "kind": "number", "unit": "单"},
        ],
        "rows": [{"cargo_owner": "HJXH", "warehouse_code": "W1", "sku_code": "A",
                  "customer_channel": "其他", "quantity": "1", "outbound_count": "1"}],
        "summary": [{"label": "实际销售出库", "value": "1", "unit": "EA"},
                    {"label": "出库单数", "value": "1", "unit": "单"}],
    }
    public = _public_business_table(table, live=True)
    assert public is not None
    assert "customer_channel" in [column["key"] for column in public["columns"]]


def test_date_grain_excludes_midnight_and_distinguishes_owner_document_keys(facts):
    rows = execute(facts, SalesAggregation(("ship_date",)))
    assert len(rows) == 1
    assert rows[0]["ship_date"] == "2026-09-09"
    assert rows[0]["quantity"] == 18 and rows[0]["outbound_count"] == 4


def test_zero_quantity_documents_are_not_filtered_from_status_groups(facts):
    rows = execute(facts, SalesAggregation(("shipment_status",)))
    by_status = {str(r["shipment_status"]): r for r in rows}
    assert set(by_status) == {"700", "900", "701", "901"}
    assert by_status["701"]["quantity"] == 0 and by_status["701"]["outbound_count"] == 1
    assert by_status["901"]["quantity"] == 0 and by_status["901"]["outbound_count"] == 1


def test_zero_document_count_is_reported_at_status_grain(facts):
    rows = execute(facts, SalesAggregation(("shipment_status",), include_zero_document_count=True))
    by_status = {str(r["shipment_status"]): r for r in rows}
    assert by_status["700"]["zero_outbound_count"] == 0
    assert by_status["701"]["zero_outbound_count"] == 1
    assert by_status["901"]["zero_outbound_count"] == 1

    plan = SalesAggregation(("shipment_status",), include_zero_document_count=True)

    class Execution:
        legacy_owner = "HJXH"

        def query(self, sql, *, purpose):
            return execute(facts, plan)

    request = SemanticRequest(
        "sales_aggregate", date(2026, 9, 9), date(2026, 9, 10),
        sales_aggregation=plan,
    )
    raw = SalesAggregateHandler(Execution(), timezone_name="Asia/Shanghai").run(request).business_table
    public = _public_business_table(raw, live=True)
    assert public is not None
    assert [column["key"] for column in public["columns"]][-1] == "zero_outbound_count"
    assert public["columns"][-1]["label"] == "零数量出库单数"


def test_sku_document_counts_are_not_added_to_make_company_document_total(facts):
    rows = execute(facts, SalesAggregation(("sku",)))
    assert sum(r["outbound_count"] for r in rows) == 5
    assert all(r["total_outbound_count"] == 4 for r in rows)
    assert sum(Decimal(str(r["quantity"])) for r in rows) == Decimal("18")


@pytest.mark.parametrize("order_types,expected", [
    (("JYCK", "HHCK"), {"JYCK": (8, 1), "HHCK": (0, 1)}),
    (("HHCK", "BFCK", "SO07"), {"HHCK": (0, 1), "BFCK": (0, 1), "SO07": (10, 1)}),
])
def test_explicit_order_type_union_keeps_every_member(facts, order_types, expected):
    rows = execute(facts, SalesAggregation(("ref_order_type",), ref_order_types=order_types))
    assert {r["ref_order_type"]: (r["quantity"], r["outbound_count"]) for r in rows} == expected


@pytest.mark.parametrize("question,order_types,dimensions", [
    ("昨天JYCK和HHCK实际销售出库数量和出库单数，按出库类型汇总", ("JYCK", "HHCK"), ("ref_order_type",)),
    ("昨天JYCK、HHCK、BFCK、QTCK实际销售出库数量和出库单数，按出库类型汇总", ("JYCK", "HHCK", "BFCK", "QTCK"), ("ref_order_type",)),
    ("昨天JYCK或SO07实际销售出库，按出库类型汇总", ("JYCK", "SO07"), ("ref_order_type",)),
    ("昨天JYCK和HHCK实际销售出库，按仓库汇总", ("JYCK", "HHCK"), ("warehouse",)),
    ("昨天JYCK和HHCK实际销售出库，按SKU汇总", ("JYCK", "HHCK"), ("sku",)),
    ("昨天JYCK和HHCK实际销售出库量", ("JYCK", "HHCK"), ()),
    ("昨天JYCK实际销售出库，按仓库汇总", ("JYCK",), ("warehouse",)),
    ("昨天JYCK实际销售出库，按出库类型汇总", ("JYCK",), ("ref_order_type",)),
])
def test_type_filters_and_grain_are_not_discarded(question, order_types, dimensions):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(question)
    assert request is not None and request.route == "sales_aggregate"
    assert request.sales_aggregation.order_types == order_types
    assert request.sales_aggregation.dimensions == dimensions
    assert request.scope.owner == "ALL"


@pytest.mark.parametrize("question", [
    "昨天除JYCK外的实际销售出库数量和出库单数，按出库类型汇总",
    "昨天JYCK的2C或HHCK的2B实际销售出库数量和出库单数",
    "昨天JYCK和UNKNOWN实际销售出库数量和出库单数",
    "昨天JYCK和HHCK销售出库TOP20 SKU",
])
def test_type_expressions_not_owned_by_fast_route_reach_general_planner(question):
    assert RequestPlanner().classify(question) is None


@pytest.mark.parametrize("kwargs", [
    {"ref_order_type": ""},
    {"ref_order_types": ("JYCK", "JYCK")},
    {"ref_order_types": ("UNKNOWN",)},
    {"ref_order_type": "JYCK", "ref_order_types": ("HHCK",)},
])
def test_ambiguous_type_set_is_rejected(kwargs):
    with pytest.raises(ValueError):
        SalesAggregation(**kwargs)


@pytest.mark.parametrize("dimensions", [(), ("warehouse",), ("sku",), ("shipment_status",),
    ("ref_order_type",), ("ship_date", "warehouse", "sku", "ref_order_type", "fulfillment_type")])
def test_every_type_subset_matches_an_independent_fact_oracle(facts, dimensions):
    source = [
        ("HJXH", "O1", "W1", "700", "JYCK", "2C", "A", Decimal("3.75")),
        ("HJXH", "O1", "W1", "700", "JYCK", "2C", "B", Decimal("4.25")),
        ("HJXH", "O2", "W1", "701", "HHCK", "2B", "A", Decimal(0)),
        ("HJXH", "O3", "W2", "901", "BFCK", "未配置", "A", Decimal(0)),
        ("B", "O1", "W1", "900", "SO07", "2C", "A", Decimal(10)),
    ]
    fields = ("cargo_owner", "document", "warehouse_code", "shipment_status", "ref_order_type", "fulfillment_type", "sku_code", "quantity")
    for size in range(1, 6):
        for selected in combinations(("JYCK", "HHCK", "BFCK", "QTCK", "SO07"), size):
            plan = SalesAggregation(dimensions, ref_order_types=selected)
            expected, documents = {}, set()
            for raw in source:
                row = dict(zip(fields, raw), ship_date="2026-09-09")
                if row["ref_order_type"] not in selected:
                    continue
                key = tuple(row[k] for k in plan.keys)
                group = expected.setdefault(key, [Decimal(0), set()])
                group[0] += row["quantity"]
                group[1].add((row["cargo_owner"], row["document"]))
                documents.add((row["cargo_owner"], row["document"]))
            actual = execute(facts, plan)
            assert {tuple(r[k] for k in plan.keys): (Decimal(str(r["quantity"])), r["outbound_count"]) for r in actual} == {
                key: (quantity, len(group_documents)) for key, (quantity, group_documents) in expected.items()}
            assert all(r["total_outbound_count"] == len(documents) for r in actual)


def test_type_filter_is_request_local_and_survives_share_analysis():
    planner = RequestPlanner()
    first = planner.classify("昨天HJXH货主JYCK和HHCK实际销售出库，按仓库汇总数量和出库单数")
    assert first.scope.owner == "HJXH"
    assert first.sales_aggregation.order_types == ("JYCK", "HHCK")
    second = planner.classify("昨天实际销售出库数量和出库单数，按出库类型汇总")
    assert second.scope.owner == "ALL" and second.sales_aggregation.order_types == ()
    third = planner.classify("昨天JYCK和HHCK实际销售出库，按仓库汇总数量和出库单数，并计算数量占比")
    assert third.route == "sales_analysis"
    assert third.sales_aggregation.order_types == ("JYCK", "HHCK")
    assert third.scope.owner == "ALL"


@pytest.mark.parametrize("period", ["昨天", "最近14天"])
def test_type_summary_wording_preserves_group_without_dimension_verb(period):
    request = RequestPlanner().classify(period + "实际销售出库，出库类型汇总")
    assert request is not None and request.route == "sales_aggregate"
    assert request.sales_aggregation.dimensions == ("ref_order_type",)


def test_conflicting_header_does_not_multiply_and_has_explicit_control(facts):
    facts.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,outbound_code,'W3',ship_at,status,ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O2'")
    rows = execute(facts, SalesAggregation(("ship_date",)))
    assert rows[0]["header_conflict_count"] == 1
    assert rows[0]["quantity"] == 18


def test_header_conflicts_outside_selected_sku_do_not_block_it(facts):
    facts.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,'B-ONLY','W2',ship_at,status,ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O2'")
    facts.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,'B-ONLY','W3',ship_at,status,ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O2'")
    facts.execute("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES ('HJXH','B-ONLY','B',1,0,'false')")
    rows=execute(facts,SalesAggregation(('sku',)),QueryScope(sku_codes=('A',)))
    assert rows[0]['header_conflict_count']==0


@pytest.mark.parametrize("question,dimension,owner", [
    ("查询昨天 HJXH 货主的有效销售出库数量和出库单数，按仓库汇总。昨天按北京时间自然日计算。", "warehouse", "HJXH"),
    ("统计 2026-09-01 至 2026-09-10 0 点前的每日有效销售出库数量和出库单数，按日期汇总。", "ship_date", "ALL"),
    ("查询昨天所有有效销售出库，按出库状态拆分数量和出库单数，并说明状态为 701 或 901 但数量为 0 的出库单是否应该保留。", "shipment_status", "ALL"),
])
def test_composite_measures_have_a_complete_reviewed_plan(question, dimension, owner):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(question)
    assert request is not None and request.route == "sales_aggregate"
    assert dimension in request.sales_aggregation.dimensions
    assert request.scope.owner == owner
    assert request.end == date(2026, 9, 10)


@pytest.mark.parametrize("question", [
    "昨天出库量和出库单数", "昨天销售出库数量和单数，排除客户甲", 
    "昨天销售出库数量和单数，按订单创建时间", "昨天销售出库数量、分配数量和差异",
    "昨天销售出库数量和单数，按城市经理汇总",
    "昨天销售出库数量和单数，按状态统计，状态为700",
    "昨天销售出库数量和单数，按状态701和901统计",
    "昨天销售出库数量为0，按仓库",
    "昨天销售出库数量和单数，数量>0",
    "昨天销售出库数量和单数，单位为吨",
])
def test_unimplemented_predicates_are_not_silently_ignored(question):
    planner = RequestPlanner()
    request = planner.classify(question)
    assert request is None


def test_reconciliation_rejects_corrupted_controls(facts):
    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            rows = execute(facts, SalesAggregation(("warehouse",)))
            rows[0]["outbound_count"] = "1.5"
            return rows
    request = SemanticRequest("sales_aggregate", date(2026, 9, 9), date(2026, 9, 10), sales_aggregation=SalesAggregation(("warehouse",)))
    with pytest.raises(SemanticRuntimeError):
        SalesAggregateHandler(Execution(), timezone_name="Asia/Shanghai").run(request)


@pytest.mark.parametrize("dimensions", [
    ("warehouse",), ("ship_date",), ("shipment_status",), ("ref_order_type",), ("sku",), (),
    ("ship_date", "cargo_owner", "warehouse", "shipment_status", "ref_order_type", "fulfillment_type", "sku"),
])
def test_reconciled_measures_and_dimensions_survive_public_table_screening(facts, dimensions):
    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            return execute(facts, SalesAggregation(dimensions))
    request = SemanticRequest("sales_aggregate", date(2026, 9, 9), date(2026, 9, 10), sales_aggregation=SalesAggregation(dimensions))
    answer = SalesAggregateHandler(Execution(), timezone_name="Asia/Shanghai").run(request)
    table = _public_business_table(answer.business_table, live=True)
    assert table is not None
    expected_keys = list(SalesAggregation(dimensions).keys)
    if "warehouse" in dimensions:
        expected_keys.append("warehouse_name")
    assert [column["key"] for column in table["columns"]] == expected_keys + ["quantity", "outbound_count"]
    assert {c["key"] for c in table["columns"]} >= {"quantity", "outbound_count"}
    assert sum(Decimal(r["quantity"]) for r in table["rows"]) == Decimal("18")
    assert table["summary"][1] == {"label": "出库单数", "value": "4", "unit": "单"}
    assert "零数量" in table["notes"][0]
    assert "数据同步时间" in table["notes"][1]
    answer.business_table["columns"].append({"key": "private_diagnostic"})
    assert _public_business_table(answer.business_table, live=True) is None


def test_null_fulfillment_is_not_silently_classified_as_2b(facts):
    rows = execute(facts, SalesAggregation(("fulfillment_type",)))
    assert {r["fulfillment_type"] for r in rows} == {"2C", "2B", "未配置"}
    selected = execute(facts, SalesAggregation(("fulfillment_type",), fulfillment_type="2B"))
    assert len(selected) == 1 and selected[0]["outbound_count"] == 1


def test_date_range_is_resolved_from_original_whitespace_not_compact_shape():
    planner = RequestPlanner()
    question = "统计2026-09-01至2026-09-10 0 点前各仓库实际销售出库"
    request = planner.classify(question)
    assert request.start == date(2026, 9, 1) and request.end == date(2026, 9, 10)


@pytest.mark.parametrize("question,dimension", [
    ("昨天销售出库数量，按状态汇总", "shipment_status"),
    ("本月每日实际销售出库数量", "ship_date"),
    ("昨天实际销售出库数量，按渠道汇总", "customer_channel"),
])
def test_grouping_request_is_not_silently_reduced_to_total(question, dimension):
    request = RequestPlanner().classify(question)
    assert request is not None and request.route == "sales_aggregate"
    assert dimension in request.sales_aggregation.dimensions


def test_platform_dimension_is_not_silently_reinterpreted_as_customer_channel():
    request = RequestPlanner().classify("昨天实际销售出库数量，按平台汇总")
    assert request is None or "customer_channel" not in request.sales_aggregation.dimensions


@pytest.mark.parametrize("verb", ["列出", "输出", "展示", "显示", "返回", "给出"])
def test_sku_count_output_verbs_are_equivalent(verb):
    request = RequestPlanner().classify(
        f"昨天实际销售出库，按仓库和渠道汇总，计算数量占比，{verb}SKU数"
    )
    assert request is not None and request.route == "sales_analysis"
    assert request.sales_aggregation.include_sku_count is True


def test_production_date_inventory_is_not_dependent_on_the_word_inventory():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify("8月3日生产的烤肠在长沙黄兴有多少")
    assert request is not None and request.route == "batch_inventory"
    assert request.production_date == date(2026, 8, 3)


@pytest.mark.parametrize("question", [
    "最近7天实际销售出库，按类型汇总",
    "最近14天实际销售出库，按类型汇总",
    "本月实际销售出库，按货主和类型汇总",
])
def test_quantity_only_grouping_uses_the_reconciled_aggregate_route(question):
    request = RequestPlanner().classify(question)
    assert request is not None and request.route == "sales_aggregate"
    assert "ref_order_type" in request.sales_aggregation.dimensions


def test_runtime_uses_actual_compiled_sql_and_resets_scope_between_requests(facts):
    class QueryClient:
        def query(self, *, sql, purpose):
            translated = sqlglot.transpile(sql, read="doris", write="sqlite")[0]
            cursor = facts.execute(translated)
            columns = [column[0] for column in cursor.description]
            rows = [dict(row) for row in cursor]
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
                "columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}}
    runtime = SemanticQueryRuntime(query_client=QueryClient(), clock=lambda: datetime(2026, 9, 10, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    first = runtime.run_if_supported("昨天HJXH货主实际销售出库数量和出库单数，按仓库汇总")
    second = runtime.run_if_supported("昨天实际销售出库数量和出库单数，按日期汇总")
    assert first["business_table"]["summary"][1]["value"] == "3"
    assert second["business_table"]["summary"][1]["value"] == "4"
    assert second["query_plan"]["scope"]["owner"] == "ALL"
    assert second["query_plan"]["grain"] == "ship_date"
    assert second["query_execution"]["live_business_result_executed"]


def test_public_column_projection_cannot_hide_required_measure():
    table = {"format": "table", "table_id": "sales_aggregate", "columns": [{"key": "quantity"}], "rows": [], "summary": []}
    assert _public_business_table(table, live=True) is None


def test_production_semantic_runtime_uses_the_single_mcp_tool():
    from app.codex_mcp_bridge import CodexMcpBridge
    assert isinstance(SemanticQueryRuntime().query_client, CodexMcpBridge)


@pytest.mark.parametrize("count", [100, 101, 500, 100000, 100001])
def test_warehouse_capacity_has_an_explicit_boundary_not_silent_truncation(count):
    class Execution:
        legacy_owner = "HJXH"
        def query(self, sql, *, purpose):
            return [{"cargo_owner": "TEST", "warehouse_code": f"W{i:04d}", "warehouse_name": f"测试仓{i}",
                "warehouse_name_variants": 1, "quantity": "1", "outbound_count": 1,
                "total_quantity": str(count), "total_outbound_count": count, "group_count": count,
                "header_conflict_count": 0, "null_quantity_count": 0, "negative_quantity_count": 0,
                "blank_key_count": 0, "latest_shipment_at": "2026-09-09 12:00:00"} for i in range(count)]
    request = SemanticRequest("sales_aggregate", date(2026, 9, 9), date(2026, 9, 10), sales_aggregation=SalesAggregation(("warehouse",)))
    handler = SalesAggregateHandler(Execution(), timezone_name="Asia/Shanghai")
    if count > 100000:
        with pytest.raises(SemanticRuntimeError) as raised:
            handler.run(request)
        assert raised.value.reason == "result_truncated"
    else:
        answer = handler.run(request)
        assert len(answer.business_table["rows"]) == count
        if count <= 500:
            table = _public_business_table(answer.business_table, live=True)
            assert table is not None


def test_sales_contract_accepts_so_family_and_padded_order_types():
    from app.outbound_contract import ref_order_type_sql

    predicate = ref_order_type_sql()
    assert "TRIM(h.ref_order_type)" in predicate
    assert "LIKE 'SO%'" in predicate
    assert "'JYCK'" in predicate and "'HHCK'" in predicate


@pytest.mark.parametrize("order_type", ["SO07", "SOABC", "SO-CHANNEL"])
def test_sales_aggregation_accepts_all_reviewed_so_prefix_variants(order_type):
    plan = SalesAggregation(ref_order_type=order_type)
    sql = compile_sales_aggregation(plan, start=date(2026, 9, 9), end=date(2026, 9, 10))
    assert order_type in sql


@pytest.mark.parametrize("window", ["昨天", "近7天", "近14天", "上月", "2026-08-25至2026-09-09"])
@pytest.mark.parametrize("group,dimensions", [
    ("仓库和SKU", {"warehouse", "sku"}),
    ("SKU与仓库", {"warehouse", "sku"}),
    ("日期、仓库和SKU", {"ship_date", "warehouse", "sku"}),
    ("仓库和SKU，按货主和出库状态", {"cargo_owner", "warehouse", "sku", "shipment_status"}),
    ("货主和出库类型", {"cargo_owner", "ref_order_type"}),
])
@pytest.mark.parametrize("suffix", ["", "，并列出出库单数"])
def test_plain_aggregates_preserve_all_coordinated_dimensions(window, group, dimensions, suffix, monkeypatch):
    from app.knowledge_activation import active_snapshot
    monkeypatch.setattr("app.query_scope.active_snapshot", active_snapshot)
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(f"查询{window}实际销售出库数量，按{group}汇总{suffix}")
    assert request is not None and request.route == "sales_aggregate"
    assert set(request.sales_aggregation.dimensions) == dimensions
    assert request.scope.owner == "ALL"


def test_composite_grouping_reconciles_real_compiled_facts(facts):
    class Client:
        def query(self, *, sql, purpose):
            cursor = facts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])
            columns = [column[0] for column in cursor.description]
            rows = [dict(row) for row in cursor]
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
                "columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}}
    runtime = SemanticQueryRuntime(query_client=Client(), clock=lambda: datetime(2026, 9, 10, 12))
    result = runtime.run_if_supported("昨天实际销售出库数量，按仓库和SKU汇总，并列出出库单数")
    public = _public_business_table(result["business_table"], live=True)
    assert public and {column["key"] for column in public["columns"]} >= {"cargo_owner", "warehouse_code", "sku_code"}
    assert len(public["rows"]) == 4
    assert sum(Decimal(row["quantity"]) for row in public["rows"]) == Decimal("18")
    assert sum(int(row["outbound_count"]) for row in public["rows"]) == 5
    assert public["summary"][1]["value"] == "4"


def test_production_product_alias_uses_finished_goods_composite_route():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(
        "查询昨天HJXH生产产品按仓库和SKU汇总实际销售出库数量，并列出出库单数。"
    )
    assert request is not None
    assert request.route == "sales_aggregate"
    assert request.finished_goods_only is True
    assert set(request.sales_aggregation.dimensions) == {"warehouse", "sku"}
    assert request.scope.owner == "HJXH"


@pytest.mark.parametrize("question", [
    "查询昨天HJXH生产产品按仓库与SKU统计实际出货量。",
    "查询昨天HJXH生产产品每个仓库每个SKU分别列出实际销售出库量。",
    "查询昨天全仓实际销售出库量按仓库和SKU汇总。",
])
def test_common_outbound_wording_variants_keep_composite_grain(question):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(question)
    assert request is not None and request.route == "sales_aggregate"
    assert request.finished_goods_only is ("生产产品" in question)
    assert set(request.sales_aggregation.dimensions) == {"warehouse", "sku"}


@pytest.mark.parametrize("question", [
    "昨天每个仓库的实际出库分别是多少？",
    "近14天各仓库实际销售出库数量。",
    "查9月1号至9月14号每个仓每个SKU的实际出货。",
])
def test_colloquial_outbound_wording_is_planned_without_model_fallback(question):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(question)
    assert request is not None
    assert request.route in {"sales_outbound_by_warehouse", "sales_aggregate"}


def test_warehouse_fulfillment_pair_is_kept_as_a_dimension():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify("昨天各仓库2C和2B实际销售出库量")
    assert request is not None and request.route == "sales_aggregate"
    assert set(request.sales_aggregation.dimensions) == {"warehouse", "fulfillment_type"}


@pytest.mark.parametrize("question", [
    "昨天各仓库2C和2B出库量",
    "昨天各仓库卖了多少货？",
    "近14天各仓库动销数量。",
    "昨天实际销售出库量和销售额按仓库汇总",
    "昨天实际销售出库量按品类汇总",
    "昨天实际销售出库量按仓库汇总并给增减",
    "昨天和前天实际销售出库量按仓库比较",
])
def test_unknown_measure_or_operator_is_not_swallowed_by_fast_route(question):
    assert RequestPlanner().classify(question) is None


@pytest.mark.parametrize("window", ["昨天", "近7天", "近14天", "上月"])
@pytest.mark.parametrize("grain,dimensions", [
    ("各仓库", {"warehouse", "fulfillment_type"}),
    ("按仓库和SKU", {"warehouse", "sku", "fulfillment_type"}),
    ("按日期、仓库和SKU", {"ship_date", "warehouse", "sku", "fulfillment_type"}),
    ("各仓库按类型", {"warehouse", "ref_order_type", "fulfillment_type"}),
])
def test_fulfillment_composition_never_loses_another_dimension(window, grain, dimensions):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    request = planner.classify(f"查询{window}实际销售出库量，{grain}和2C/2B分别统计")
    assert request is not None and request.route == "sales_aggregate"
    assert set(request.sales_aggregation.dimensions) == dimensions
    assert request.scope.owner == "ALL"
    assert request.fulfillment_type is None


@pytest.mark.parametrize("extra", ["按金额排序", "再给库存", "排除客户甲", "销售订单数", "按仓库和城市汇总"])
def test_composite_grouping_keeps_unsupported_requirements(extra):
    assert RequestPlanner().classify("昨天实际销售出库数量，按仓库和SKU汇总，并列出出库单数，" + extra) is None
