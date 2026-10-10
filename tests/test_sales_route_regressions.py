from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agent_core.handlers.sales import SalesHandler
from app.agent_core.planning import RequestPlanner
from app.query_scope import QueryScope
from app.chat_api import _public_business_table, _model_verified_numeric_columns
from app.codex_agent import reviewed_clarification_for_question
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
import pytest


class _NoopExecution:
    legacy_owner = "HJXH"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def query(self, sql: str, *, purpose: str | None = None):
        self.calls.append(sql)
        return {"rows": []}


def _planner() -> RequestPlanner:
    return RequestPlanner(
        clock=lambda _zone: datetime(2026, 9, 9, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    )


def test_confirmed_sales_phrases_have_deterministic_routes() -> None:
    planner = _planner()

    grouped = planner.classify("\u6628\u65e5\u6309\u7c7b\u578b\u548c 2C/2B \u7edf\u8ba1\u6709\u6548\u9500\u552e\u51fa\u5e93")
    assert grouped is not None
    assert grouped.route == "sales_outbound_by_type"

    ranked = planner.classify("\u6628\u65e5 JYCK \u4ea7\u6210\u54c1\u51fa\u5e93 TOP20")
    assert ranked is not None
    assert ranked.route == "sales_outbound_top_sku"
    assert ranked.ref_order_type == "JYCK"
    assert ranked.finished_goods_only is True


def test_chinese_date_and_table_request_routes_to_warehouse_sales() -> None:
    request = _planner().classify("请查询2026年9月8日各仓库实际销售出库量，按仓库展示表格")
    assert request is not None
    assert request.route == "sales_outbound_by_warehouse"
    assert request.start == date(2026, 9, 8)
    assert request.show_warehouses is True
    assert request.scope.owner == "ALL"


def test_sales_completion_synonym_routes_without_slow_model_fallback() -> None:
    request = _planner().classify("请查询昨天各仓库的销售完成量，按仓库展示表格")
    assert request is not None
    assert request.route == "sales_outbound_by_warehouse"
    assert request.show_warehouses is True


def test_explicit_actual_outbound_composite_request_keeps_both_measures(monkeypatch) -> None:
    monkeypatch.setattr("app.query_scope.active_snapshot", lambda root: (Path(__file__).resolve().parents[1], None))
    question = "昨天实际销售出库总量和有效出库单数"
    # The reviewed aggregate now computes quantity and distinct outbound counts.
    request = _planner().classify(question)
    assert request is not None and request.route == "sales_aggregate"
    assert reviewed_clarification_for_question(question) is None
    assert reviewed_clarification_for_question("昨天发货量") is not None


def test_screenshot_owner_and_fulfillment_grouping_is_not_order_type() -> None:
    request = _planner().classify("昨日按货主和 2C/2B 统计有效销售出库")
    assert request is not None
    assert request.route == "sales_outbound_by_owner"
    assert request.scope.owner == "ALL"
    sql = SalesHandler(_NoopExecution(), timezone_name="Asia/Shanghai")._sales_by_type_sql(
        request.start, request.end, group_by_owner=True,
    )
    assert "GROUP BY h.cargo_owner, h.fulfillment_type" in sql
    assert "GROUP BY h.ref_order_type, h.fulfillment_type" not in sql


def test_group_reconciliation_uses_unrounded_decimal_values() -> None:
    class ResultExecution(_NoopExecution):
        def query(self, sql, *, purpose=None):
            return [
                {"cargo_owner": "A", "fulfillment_type": "2B", "outbound_ea": "1234.25", "total_ea": "2345.50", "group_count": 2},
                {"cargo_owner": "B", "fulfillment_type": "2C", "outbound_ea": "1111.25", "total_ea": "2345.50", "group_count": 2},
            ]
    handler = SalesHandler(ResultExecution(), timezone_name="Asia/Shanghai")
    request = SemanticRequest("sales_outbound_by_owner", date(2026, 9, 8), date(2026, 9, 9))
    result = handler._run_sales_outbound_by_type(request)
    assert result.business_table["table_id"] == "sales_outbound_by_owner"
    assert len(result.business_table["rows"]) == 2
    public_table = _public_business_table(result.business_table, live=True)
    assert public_table is not None
    assert [column["key"] for column in public_table["columns"]] == [
        "cargo_owner", "fulfillment_type", "quantity",
    ]
    assert len(public_table["rows"]) == 2


def test_arbitrary_verified_projection_is_screened_without_model_values() -> None:
    table = _public_business_table({
        "format": "table", "table_id": "model_verified", "title": "查询结果",
        "caption": "宿主已校验的实时结果", "columns": ["warehouse", "quantity"],
        "rows": [{"warehouse": "长沙黄兴", "quantity": "12.50"}],
    }, live=True)
    assert table is not None
    assert table["table_id"] == "model_verified"
    assert [column["key"] for column in table["columns"]] == ["col_1", "col_2"]
    # Generic model aliases never become user-facing database identifiers.
    assert [column["label"] for column in table["columns"]] == ["结果列 1", "数量"]
    assert table["rows"] == [{"col_1": "长沙黄兴", "col_2": "12.50"}]
    assert _public_business_table({
        "format": "table", "table_id": "model_verified", "title": "查询结果",
        "caption": "", "columns": ["warehouse"], "rows": [{"warehouse": "<script>"}],
    }, live=True) is None


def test_arbitrary_verified_projection_supports_non_identifier_column_names() -> None:
    table = _public_business_table({
        "format": "table", "table_id": "model_verified", "title": "查询结果",
        "caption": "", "columns": ["仓库名称", "SUM(实发数量)", "SUM(实发数量) 2"],
        "rows": [{"仓库名称": "长沙黄兴", "SUM(实发数量)": "-1.5", "SUM(实发数量) 2": "2"}],
    }, live=True)
    assert table is not None
    assert [column["key"] for column in table["columns"]] == ["col_1", "col_2", "col_3"]
    assert table["rows"][0] == {"col_1": "长沙黄兴", "col_2": "-1.5", "col_3": "2"}


def test_generic_projection_preserves_identifiers_dates_and_decimal_strings() -> None:
    columns = ["sku_code", "warehouse_code", "order_code", "quantity", "business_date", "count"]
    row = dict(zip(columns, ["00123", "1E10", "900719925474099312345", "1.2300", "20260911", 7]))
    table = _public_business_table({
        "format": "table", "table_id": "model_verified", "columns": columns, "rows": [row],
    }, live=True)
    assert table is not None
    assert [column["kind"] for column in table["columns"]] == ["text"] * 5 + ["number"]
    assert list(table["rows"][0].values()) == ["00123", "1E10", "900719925474099312345", "1.2300", "20260911", "7"]


def test_generic_projection_keeps_exact_alias_and_distinguishes_missing_from_null() -> None:
    raw = {"format": "table", "table_id": "model_verified", "columns": [" code "], "rows": [{" code ": "00123"}]}
    assert _public_business_table(raw, live=True)["rows"] == [{"col_1": "00123"}]
    assert _public_business_table({**raw, "rows": [{}]}, live=True) is None
    assert _public_business_table({**raw, "rows": [{" code ": None}]}, live=True)["rows"] == [{"col_1": None}]


def test_generic_projection_uses_full_result_types_across_pages() -> None:
    rows = [{"value": 12}, {"value": "00123"}, {"value": None}]
    kinds = _model_verified_numeric_columns(["value"], rows)
    assert kinds == [False]
    for row in rows:
        table = _public_business_table({
            "format": "table", "table_id": "model_verified", "columns": ["value"],
            "rows": [row], "numeric_columns": kinds,
        }, live=True)
        assert table is not None
        assert table["columns"][0]["kind"] == "text"
        assert table["rows"][0]["col_1"] == (None if row["value"] is None else str(row["value"]))


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_generic_projection_rejects_nonfinite_or_ambiguous_native_values(value) -> None:
    assert _public_business_table({
        "format": "table", "table_id": "model_verified", "columns": ["value"], "rows": [{"value": value}],
    }, live=True) is None


def test_duplicate_groups_and_missing_controls_are_rejected() -> None:
    class ResultExecution(_NoopExecution):
        def query(self, sql, *, purpose=None):
            return [{"cargo_owner": "A", "fulfillment_type": "2B", "outbound_ea": "1", "total_ea": "2", "group_count": 2}] * 2
    request = SemanticRequest("sales_outbound_by_owner", date(2026, 9, 8), date(2026, 9, 9))
    with pytest.raises(SemanticRuntimeError):
        SalesHandler(ResultExecution(), timezone_name="Asia/Shanghai")._run_sales_outbound_by_type(request)


def test_sales_sql_pins_actual_quantity_and_explicit_scopes() -> None:
    execution = _NoopExecution()
    handler = SalesHandler(execution, timezone_name="Asia/Shanghai")

    grouped_sql = handler._sales_by_type_sql(
        datetime(2026, 9, 8).date(), datetime(2026, 9, 9).date()
    )
    assert "terms_of_shipment" in grouped_sql
    assert "SUM(COALESCE(d.ship_qty, 0))" in grouped_sql
    assert "allocate_qty" not in grouped_sql

    ranked_sql = handler._sales_top_sku_sql(
        datetime(2026, 9, 8).date(),
        datetime(2026, 9, 9).date(),
        20,
        ref_order_type="JYCK",
        finished_goods_only=True,
    )
    assert "TRIM(h.ref_order_type) = 'JYCK'" in ranked_sql
    assert "TRIM(CAST(sku_group AS STRING)) = '50'" in ranked_sql
    assert "finished_skus.sku_code = TRIM(d.sku_code)" in ranked_sql
    assert "SUM(COALESCE(d.ship_qty, 0))" in ranked_sql
