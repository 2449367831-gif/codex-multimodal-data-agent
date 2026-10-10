"""Submit quantity-filter questions and reconcile every returned group.

Business rows stay in memory. Independent references use only the approved MCP
bridge and the source ledger, not the production query compiler.
"""

import argparse
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient
from tests.acceptance.check_composed_operators_live import compare_threshold, independent_sql
from tests.acceptance.check_partition_ranking_live import complete_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    if not 30 <= args.timeout <= 900:
        parser.error("timeout must be between 30 and 900 seconds")
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    end = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    threshold = Decimal(1000)
    for days in (7, 14):
        start = end - timedelta(days=days)
        sql = independent_sql("threshold", start, end, threshold=threshold)
        before = validate_doris_result(bridge.query(sql=sql, purpose="independent quantity filter reference")).rows
        session = client.request("/v1/session")
        payload = {"question": f"查询近{days}个完整自然日实际销售出库量大于1000的仓库、SKU及数量，用表格展示。",
                   "conversation_id": session["conversation_id"]}
        key = "quantity-filter-acceptance-" + uuid4().hex
        started = time.monotonic()
        task = client.request("/v1/tasks", payload, session["csrf_token"], key)
        identifier = task["task_id"]
        replay = client.request("/v1/tasks", payload, session["csrf_token"], key)
        assert replay["task_id"] == identifier, "idempotency_failed"
        while task["state"] in {"queued", "running"} and time.monotonic() - started < args.timeout:
            time.sleep(2)
            task = client.request("/v1/tasks/" + identifier)
        assert task["state"] == "completed", "task_not_completed"
        assert task["result"]["result_state"] == "live_success", "task_not_live"
        table = task["result"]["business_table"]
        compare_threshold(table, complete_rows(client, table), before, threshold)
        after = validate_doris_result(bridge.query(sql=sql, purpose="independent quantity filter stability")).rows
        assert after == before, "source_changed"
        print(json.dumps({"days": days, "task_id": identifier, "verification": "passed",
                          "checks": ["all_groups", "independent_values", "post_aggregate_filter",
                                     "units", "idempotency", "source_stability"]}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
