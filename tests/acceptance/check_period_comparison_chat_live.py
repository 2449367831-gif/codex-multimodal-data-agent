"""Compare live chat period answers with an independent Doris source ledger.

Only task identifiers, check names and durations are printed. Business rows
remain in memory and are never stored as fixtures or output artifacts.
"""

import argparse
from datetime import datetime, timedelta
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
from tests.acceptance.check_composed_operators_live import compare_periods, independent_sql
from tests.acceptance.check_partition_ranking_live import complete_rows


def question(periods, group, *, chinese=False):
    def span(start, end):
        last = end - timedelta(days=1)
        if chinese:
            return f"{start.year}年{start.month}月{start.day}日至{last.year}年{last.month}月{last.day}日"
        return f"{start.isoformat()}到{last.isoformat()}"

    intervals = "和".join(span(*period) for period in periods)
    return f"查询{intervals}{group}日均实际销售出库量对比，用表格展示。"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    if not 30 <= args.timeout <= 900:
        parser.error("timeout must be between 30 and 900 seconds")
    _hide_windows_subprocesses()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    # Unequal lengths expose accidentally using one denominator for both sides.
    periods = ((today - timedelta(days=15), today - timedelta(days=8)),
               (today - timedelta(days=7), today - timedelta(days=1)))
    cases = [("iso_each", periods, "各仓库", False),
             ("chinese_every", periods, "每个仓库", True),
             ("reversed_operands", periods[::-1], "所有仓库", False)]
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    with bridge.request_session() as source:
        for case, windows, group, chinese in cases:
            sql = independent_sql("period", *windows[0], second=windows[1])
            before = validate_doris_result(source.query(sql=sql, purpose="independent period comparison reference")).rows
            body = {"conversation_id": session["conversation_id"], "question": question(windows, group, chinese=chinese)}
            key = "period-acceptance-" + uuid4().hex
            task = client.request("/v1/tasks", body, session["csrf_token"], key)
            identifier = task["task_id"]
            print(json.dumps({"case": case, "task_id": identifier, "state": task["state"]}), flush=True)
            started = time.monotonic()
            while task["state"] in {"queued", "running"} and time.monotonic() - started < args.timeout:
                time.sleep(2)
                task = client.request("/v1/tasks/" + identifier)
            assert task["state"] == "completed", "task_not_completed"
            result = task["result"]
            assert result["result_state"] in {"live_success", "no_data"}, "task_not_live"
            table = result["business_table"]
            rows = complete_rows(client, table)
            compare_periods(table, rows, before, windows)
            after = validate_doris_result(source.query(sql=sql, purpose="independent period comparison stability")).rows
            assert before == after, "source_changed"
            replay = client.request("/v1/tasks", body, session["csrf_token"], key)
            assert replay == task, "idempotent_replay_changed"
            restored = client.request("/v1/session")
            assert restored["conversation_id"] == session["conversation_id"], "conversation_changed"
            assert client.request("/v1/tasks/" + identifier) == task, "restored_task_changed"
            print(json.dumps({"case": case, "task_id": identifier, "verification": "passed",
                              "elapsed_seconds": round(time.monotonic() - started, 2),
                              "checks": ["all_groups", "independent_values", "units", "missingness",
                                         "signed_difference", "source_stability", "idempotent_replay", "session_restore"]}), flush=True)
            session = restored


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
