"""Missing batch records and NULL quantities must never become zero stock."""

from datetime import date
from decimal import Decimal

import pytest
import sqlglot
from sqlglot import exp

from app.agent_core.handlers.batch import BatchInventoryHandler
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import _public_business_table
from app.query_scope import QueryScope


class Execution:
    legacy_owner = "HJXH"
    succeeded = 1

    def __init__(self, row):
        self.row = row

    def query(self, sql, *, purpose):
        self.sql = sql
        return [self.row]

    def evidence(self):
        return {"live_business_result_executed": True}


def source_row(**changes):
    return {
        "current_row_count": 2, "current_sku_count": 1,
        "current_stock_value_count": 2, "current_actual_value_count": 2,
        "current_stock_ea": Decimal("12.125"), "current_actual_ea": Decimal("9.125"),
        "current_freshness": "2026-09-14 12:00:00",
        "latest_backup_date": None, "backup_row_count": 0, "backup_sku_count": 0,
        "backup_stock_value_count": 0, "backup_actual_value_count": 0,
        "backup_stock_ea": None, "backup_actual_ea": None,
        **changes,
    }


def run(row):
    execution = Execution(row)
    handler = BatchInventoryHandler(execution, timezone_name="Asia/Shanghai")
    request = SemanticRequest("batch_inventory", date(2026, 8, 3), date(2026, 8, 4),
                              production_date=date(2026, 8, 3), scope=QueryScope())
    answer = handler._run_batch_inventory(request)
    result = SemanticQueryRuntime._result(request, answer, execution, question="批次库存", context_reused=False)
    return answer, result, execution


def test_complete_measured_stock_keeps_precision():
    answer, result, _ = run(source_row())
    table = _public_business_table(result["business_table"], live=True)
    assert table["rows"][0]["current_stock"] == "12.125"
    assert table["rows"][0]["available_stock"] == "9.125"
    assert "12.125 EA" in answer
    assert "2026-09-14 12:00:00" in answer
    assert result["result_state"] == "live_success"


def test_measured_zero_is_not_missing():
    answer, result, _ = run(source_row(current_stock_ea=0, current_actual_ea=0))
    assert "当前库存：0 EA" in answer
    assert result["business_table"]["rows"][0]["current_stock"] == "0"
    assert result["result_state"] == "live_success"


@pytest.mark.parametrize("value_count,value", [(0, None), (0, 0), (1, "5.125")])
def test_null_or_partial_measure_is_not_a_complete_quantity(value_count, value):
    answer, result, _ = run(source_row(current_actual_value_count=value_count, current_actual_ea=value))
    table = _public_business_table(result["business_table"], live=True)
    assert table["rows"][0]["available_stock"] is None
    assert table["rows"][0]["current_stock"] == "12.125"
    assert "实际库存数量存在缺失" in answer
    assert all(item["label"] != "当前实际库存" for item in table["summary"])


def test_partial_primary_measure_is_not_displayed_as_complete():
    answer, result, _ = run(source_row(current_stock_value_count=1))
    assert result["business_table"]["rows"][0]["current_stock"] is None
    assert "当前库存无法完整确认" in answer


@pytest.mark.parametrize("historical", [False, True])
def test_absent_current_records_stay_no_data_even_with_historical_zero(historical):
    row = source_row(current_row_count=0, current_sku_count=0, current_stock_value_count=0,
                     current_actual_value_count=0, current_stock_ea=None, current_actual_ea=None,
                     current_freshness=None)
    if historical:
        row.update(latest_backup_date="2026-08-25", backup_row_count=1, backup_sku_count=1,
                   backup_stock_value_count=1, backup_actual_value_count=1,
                   backup_stock_ea=0, backup_actual_ea=0)
    answer, result, _ = run(row)
    assert result["result_state"] == "no_data"
    table = _public_business_table(result["business_table"], live=True)
    assert table is not None
    assert "当前库存：0" not in answer and "当前实际库存：0" not in answer
    assert "不能据此确认库存为零" in answer
    if historical:
        assert table["rows"][0]["current_stock"] is None
        assert table["rows"][0]["snapshot_stock"] == "0"
        assert "不替代当前库存" in answer
    else:
        assert table["rows"] == []


@pytest.mark.parametrize("changes", [
    {"current_row_count": True}, {"current_row_count": "1.5"}, {"current_row_count": -1},
    {"current_sku_count": 3}, {"current_sku_count": 0}, {"current_stock_value_count": 3},
    {"current_stock_value_count": 0, "current_stock_ea": 100},
    {"current_stock_ea": None}, {"current_stock_ea": -1}, {"current_stock_ea": "NaN"},
    {"current_freshness": "not a date"}, {"latest_backup_date": "2026-08-25"},
])
def test_inconsistent_evidence_is_not_displayed(changes):
    with pytest.raises(SemanticRuntimeError, match="完整性校验"):
        run(source_row(**changes))


def test_missing_evidence_field_fails_closed():
    row = source_row()
    row.pop("current_stock_value_count")
    with pytest.raises(SemanticRuntimeError):
        run(row)


def test_query_counts_nonnull_values_in_both_fact_sources():
    _, _, execution = run(source_row())
    tree = sqlglot.parse_one(execution.sql, read="doris")
    ctes = {cte.alias: cte.this for cte in tree.find_all(exp.CTE)}
    for name, prefix, alias in (("current_total", "current", "i"), ("backup_total", "backup", "b")):
        projections = {item.alias: item.this for item in ctes[name].expressions}
        for measure, column in (("stock", "in_stock_quantity"), ("actual", "actual_quantity")):
            counter = projections[f"{prefix}_{measure}_value_count"]
            assert isinstance(counter, exp.Count)
            assert counter.this.table == alias and counter.this.name == column
            assert isinstance(projections[f"{prefix}_{measure}_ea"], exp.Sum)


def test_no_current_inventory_survives_chat_projection_without_asking_again(tmp_path):
    from fastapi.testclient import TestClient
    from app.chat_api import IdentityResolver, create_chat_app
    from app.chat_sessions import ChatSessionStore
    from app.tests.test_chat_api import FakeChatRunner

    _, result, _ = run(source_row(
        current_row_count=0, current_sku_count=0, current_stock_value_count=0,
        current_actual_value_count=0, current_stock_ea=None, current_actual_ea=None,
        current_freshness=None, latest_backup_date="2026-08-25", backup_row_count=1,
        backup_sku_count=1, backup_stock_value_count=1, backup_actual_value_count=0,
        backup_stock_ea=0,
    ))

    class Runner(FakeChatRunner):
        def run(self, question, **kwargs):
            return result

    app = create_chat_app(Runner(), session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
                          identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        response = client.post("/v1/chat", headers=headers, json={
            "question": "8月3日生产的烤肠在长沙黄兴的库存", "conversation_id": conversation,
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload["result_state"] == "no_data"
        assert not payload["clarification"]["required"]
        assert payload["business_table"]["rows"][0]["current_stock"] is None
        assert payload["business_table"]["rows"][0]["available_stock"] is None
        assert payload["business_table"]["rows"][0]["snapshot_stock"] == "0"
        assert "不能据此确认库存为零" in payload["business_result"]["text"]
        assert "current_stock_value_count" not in response.text
        assert "scm_warehous_manage" not in response.text


@pytest.mark.parametrize("quantities,expected,state", [
    ([], None, "no_data"), ([None], None, "live_success"),
    ([0], "0", "live_success"), ([1.125, 2], "3.125", "live_success"),
    ([1.125, None], None, "live_success"),
])
def test_compiled_query_preserves_source_missingness(quantities, expected, state):
    import sqlite3

    handler = BatchInventoryHandler(Execution({}), timezone_name="Asia/Shanghai")
    sql = handler._batch_sql(date(2026, 8, 3))
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
        db.execute("CREATE TABLE ywzt_ods.master_sku (sku_code TEXT, updated_date TEXT, version INT, "
                   "id INT, sku_group TEXT, status TEXT, sku_name TEXT)")
        db.execute("INSERT INTO ywzt_ods.master_sku VALUES ('TEST-SKU','2026-09-14',1,1,'50','0','烤肠')")
        # Duplicate source dimension versions must not multiply inventory facts.
        db.execute("INSERT INTO ywzt_ods.master_sku VALUES ('TEST-SKU','2026-09-13',0,0,'50','0','烤肠')")
        schema = ("sku_code TEXT, cargo_owner_id TEXT, wms_code TEXT, production_date TEXT, "
                  "status TEXT, inventory_status TEXT, in_stock_quantity REAL, actual_quantity REAL, "
                  "stock_change_time TEXT")
        db.execute(f"CREATE TABLE ywzt_ods.scm_warehous_manage ({schema})")
        db.execute(f"CREATE TABLE ywzt_ods.scm_warehous_manage_backups ({schema}, backups_date TEXT, _fdl_marked_deleted TEXT)")
        def record(quantity, production="2026-08-03 12:00:00", warehouse="10114790"):
            return ("TEST-SKU", "HJXH", warehouse, production, "0", "ZP", quantity, None, "2026-09-14 12:00:00")
        db.executemany("INSERT INTO ywzt_ods.scm_warehous_manage VALUES (?,?,?,?,?,?,?,?,?)",
                       [record(q) for q in quantities] + [record(9999, "2026-08-04 00:00:00"),
                                                        record(9999, warehouse="OTHER")])
        row = dict(db.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0]).fetchone())
    answer, result, _ = run(row)
    assert row["current_row_count"] == len(quantities)
    assert row["current_stock_value_count"] == sum(q is not None for q in quantities)
    assert result["result_state"] == state
    table = _public_business_table(result["business_table"], live=True)
    if quantities:
        value = table["rows"][0]["current_stock"]
        assert (None if value is None else Decimal(value)) == (None if expected is None else Decimal(expected))
        assert table["rows"][0]["available_stock"] is None
    else:
        assert table["rows"] == []
        assert "不能据此确认库存为零" in answer
