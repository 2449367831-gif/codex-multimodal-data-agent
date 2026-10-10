from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent_core.runtime import SemanticQueryRuntime
from tests.acceptance.check_model_inventory_live import QUESTION


class Client:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    def query(self, *, sql, purpose):
        self.sql.append(sql)
        return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
                "columns": list(self.rows[0]) if self.rows else [],
                "rows": self.rows, "row_count": len(self.rows), "truncated": False}}


def runtime(rows):
    return SemanticQueryRuntime(query_client=Client(rows),
        clock=lambda zone: datetime(2026, 9, 18, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")))


def row(owner, available, transit, *, negative_warehouse="0"):
    return {
        "cargo_owner": owner, "available_ea": str(available), "in_transit_ea": str(transit),
        "finished_goods_ea": str(available + transit), "source_row_count": "2",
        "negative_warehouse_count": negative_warehouse, "latest_freshness": "2026-09-18 08:30:00",
        "total_available_ea": "140", "total_in_transit_ea": "60", "total_finished_goods_ea": "200",
        "owner_count": "2", "invalid_owner_count": "0", "total_negative_warehouse_count": "0",
        "total_latest_freshness": "2026-09-18 08:30:00",
    }


def test_explicit_owner_inventory_question_has_reviewed_route_and_table():
    service = runtime([row("01", 90, 10), row("02", 50, 50)])
    plan = service.classify(QUESTION)
    assert plan is not None and plan.route == "current_inventory_owner_summary"
    result = service.execute_plan(plan, question=QUESTION)
    table = result["business_table"]
    assert result["result_state"] == "live_success"
    assert table["table_id"] == "current_inventory_owner_summary"
    assert table["rows"][0]["cargo_owner"] == "01"
    assert table["rows"][0]["in_transit_share"] == "10"
    assert table["rows"][0]["inventory_share"] == "50"
    assert "scm_warehous_manage" in service.query_client.sql[0]
    assert "sku_inventory" not in service.query_client.sql[0]


def test_owner_inventory_quality_failure_never_publishes_rows():
    service = runtime([row("01", 90, 10), row("02", 50, 50, negative_warehouse="1")])
    with __import__("pytest").raises(Exception) as captured:
        service.execute_plan(service.classify(QUESTION), question=QUESTION)
    assert captured.value.reason == "inventory_quality_failed"
