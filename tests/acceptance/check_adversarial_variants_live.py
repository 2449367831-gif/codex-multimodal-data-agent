"""Exercise natural-language variants against the live chat service.

The script prints only task IDs, public state, and assertion categories. It does
not persist or print business rows, SQL, source names, or model evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.evaluate_generalization import ChatClient  # noqa: E402


VARIANTS = (
    "帮我查一下上周线下渠道实际销售出库的数量、出库单量和箱数，用表格展示。",
    "昨天各仓库实际销售出库量，不含退货，按仓库汇总。",
    "昨天各仓库实际销售出库了多少。",
    "帮我看昨天各仓卖出去多少。",
    "请统计最近7个完整自然日各仓库实际销售出库量，返回仓库和数量表格。",
    "最近两周各仓销售出库量。",
    "昨天各仓库销售出库实际发了多少，按仓库汇总，不要分配数量。",
    "上月库存周转是多少？按当前可用成品库存加在途，除以该期间实际销售出库成品日均数量。",
    "上月每个仓的库存周转天数。",
    "查询8月3日生产的成品在长沙黄兴仓当前库存，没数据就明确说明，不要猜零。",
    "长沙黄兴8.3生产日期库存。",
    "本月截至昨天各仓库实际销售出库量。",
    "昨天线下渠道各仓实际销售出库数量和出库单数。",
    "昨天各仓销售出库数量，排除逻辑删除单据。",
    "昨天线上线下各渠道实际出库数量和出库单数，用表格展示。",
    "昨天所有出库类型实际出库数量和单数，按出库类型汇总。",
    "昨天调拨出库量。",
    "昨天DMS积分兑换空气炸锅数量。",
    "昨天实际销售出库数量最高的20个SKU，按实发数量降序列表格。",
    "统计2026年9月1日至2026年9月14日各仓库实际销售出库量，用表格展示。",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("questions", nargs="*")
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 1800:
        parser.error("timeout must be between 30 and 1800 seconds")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    questions = tuple(args.questions) or VARIANTS
    reports = []
    for index, question in enumerate(questions, 1):
        started = time.monotonic()
        task = client.request(
            "/v1/tasks",
            {"question": question, "conversation_id": session["conversation_id"]},
            session["csrf_token"],
            "adversarial-" + uuid4().hex,
        )
        task_id = task["task_id"]
        print(json.dumps({"case": index, "task_id": task_id, "state": task["state"]}), flush=True)
        last_progress = started
        while task["state"] in {"queued", "running"} and time.monotonic() - started < args.timeout:
            time.sleep(2)
            task = client.request("/v1/tasks/" + task_id)
            if time.monotonic() - last_progress >= 20:
                last_progress = time.monotonic()
                print(json.dumps({"case": index, "task_id": task_id, "state": task["state"],
                                  "elapsed_seconds": round(last_progress - started, 1)}), flush=True)
        if task["state"] in {"queued", "running"}:
            print(json.dumps({"verification": "incomplete", "case": index, "task_id": task_id,
                              "state": task["state"], "reason": "observation_deadline"}), flush=True)
            return 2
        result = task.get("result") or {}
        failures = []
        if task.get("state") != "completed":
            failures.append("task_not_completed")
        if result.get("result_state") not in {"live_success", "no_data"}:
            failures.append("not_terminal_live_state")
        clarification = result.get("clarification") or {}
        if clarification.get("required"):
            failures.append("unexpected_clarification")
        if result.get("result_state") == "live_success":
            table = result.get("business_table")
            if not isinstance(table, dict) or not table.get("columns") or not table.get("rows"):
                failures.append("missing_business_table")
        wire = json.dumps(result, ensure_ascii=False).casefold()
        if any(marker in wire for marker in ("select ", "ywzt_ods.", "dms_ods.", "traceback (", "password=")):
            failures.append("internal_details_exposed")
        reports.append({"case": index, "task_id": task_id, "state": task.get("state"),
                        "result_state": result.get("result_state"), "failures": failures,
                        "elapsed_seconds": round(time.monotonic() - started, 1)})
        print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
        if failures:
            print(json.dumps({"verification": "failed", "reports": reports}, ensure_ascii=False), flush=True)
            return 1
    print(json.dumps({"verification": "passed", "cases": len(reports),
                      "reports": reports}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
