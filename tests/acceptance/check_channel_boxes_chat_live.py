"""Reconcile ordinary business wording and every public page, in memory only."""

import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient
from tests.acceptance.check_outbound_boxes_live import reference_sql, independently_compare
from tests.acceptance.check_partition_ranking_live import complete_rows, submit


def main():
    _hide_windows_subprocesses()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = today - timedelta(days=today.weekday())
    start = end - timedelta(days=7)
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    sql = reference_sql(start, end, channel="线下", all_actual=True, finished_goods=False)
    before = validate_doris_result(bridge.query(sql=sql, purpose="channel boxes independent reference")).rows
    task = submit(client, session, "看下上周线下渠道出库数量，单量，箱数")
    assert task["state"] == "completed" and task["http_status"] == 200, "task_incomplete"
    result = task["result"]
    assert result["result_state"] in {"live_success", "no_data"}, "not_live"
    assert not result["clarification"]["required"], "unexpected_clarification"
    table = result.get("business_table")
    assert isinstance(table, dict), "missing_table"
    table = {**table, "rows": complete_rows(client, table)}
    after = validate_doris_result(bridge.query(sql=sql, purpose="channel boxes source stability")).rows
    assert before == after, "source_changed"
    checks = independently_compare(table, before)
    checks["channel_scope"] = all(row["customer_channel"] == "线下" for row in table["rows"])
    checks["title_scope"] = "成品" not in table["title"]
    checks["all_public_pages"] = len(table["rows"]) == len(before)
    print(json.dumps({"task_id": task["task_id"], "checks": checks,
                      "verification": "passed" if all(checks.values()) else "failed"}), flush=True)
    assert all(checks.values()), "reconciliation_failed"


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
