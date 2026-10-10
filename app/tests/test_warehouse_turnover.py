from datetime import date

import pytest

from app.agent_core.handlers.turnover import TurnoverHandler
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.chat_api import _public_business_table
from app.query_scope import QueryScope


class CheckedRows:
    legacy_owner = "HJXH"
    scope = QueryScope()

    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def query(self, sql, *, purpose):
        self.sql = sql
        return self.rows


def row(code="W1", inventory="140", sales="140", turnover="14"):
    return {
        "wms_code": code,
        "warehouse_name": code,
        "inventory_ea": inventory,
        "sales_ea": sales,
        "null_sales_quantity_count": 0,
        "negative_sales_quantity_count": 0,
        "inventory_latest_freshness": "2026-09-15 10:00:00",
        "complete_natural_days": 14,
        "turnover_days": turnover,
        "inventory_present": 1,
        "warehouse_mapping_present": 1,
    }


def run(rows):
    execution = CheckedRows(rows)
    handler = TurnoverHandler(execution, timezone_name="Asia/Shanghai")
    request = SemanticRequest(
        route="realtime_turnover_by_warehouse",
        start=date(2026, 9, 1),
        end=date(2026, 9, 15),
        turnover_grain="warehouse",
    )
    return handler._run_realtime_turnover_by_warehouse(request), execution.sql


def test_warehouse_turnover_keeps_anomalies_without_losing_valid_rows():
    answer, sql = run([
        row(),
        row("W2", inventory="-10", sales="140", turnover="-1"),
        row("W3", inventory="30", sales="0", turnover=None),
    ])
    table = _public_business_table(answer.business_table, live=True)
    assert table is not None
    assert len(table["rows"]) == 3
    assert table["rows"][0]["turnover_state"] == "正常"
    assert table["rows"][1]["inventory"] == "-10"
    assert table["rows"][1]["turnover_days"] is None
    assert table["rows"][1]["turnover_state"] == "库存异常"
    assert table["rows"][2]["turnover_days"] is None
    assert table["rows"][2]["turnover_state"] == "无动销"
    assert "i.ship_qty" in sql
    assert "d.request_qty" in sql
    import sqlglot
    for cte in sqlglot.parse_one(sql, read="doris").find_all(sqlglot.exp.CTE):
        if not cte.alias.startswith("inv_") and cte.alias != "inventory_current":
            assert "allocate_qty" not in cte.sql()
    assert "2026-09-01 00:00:00" in sql
    assert "2026-09-15 00:00:00" in sql


def test_warehouse_turnover_rejects_formula_mismatch():
    with pytest.raises(SemanticRuntimeError) as caught:
        run([row(turnover="99")])
    assert caught.value.reason == "warehouse_turnover_formula_mismatch"


def test_warehouse_turnover_rejects_invalid_sales_quantities():
    invalid = row()
    invalid["null_sales_quantity_count"] = 1
    with pytest.raises(SemanticRuntimeError):
        run([invalid])
