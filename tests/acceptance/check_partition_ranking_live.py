"""Holdout for per-partition rankings, with an independent in-memory oracle."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_generalization import ChatClient, _label, _number


def question(top):
    return (
        "按已发布的成品实际销售出库口径，统计昨天完整自然日所有货主、所有仓库的数据，日期按北京时间。"
        f"每个货主和仓库分别取实发数量最高的前{top}个SKU；同量时按SKU编码升序打破并列，"
        "每组排名从1开始连续编号。先按货主编码、仓库编码、SKU编码汇总实发数量，再排名。"
        "数量占比的分母必须是选出前几名之前，该货主该仓库全部SKU的实发数量，不能只用前几名合计。"
        "分母为零时标注分母为零。需要两张业务表：第一张标题为各仓排名，"
        "列为货主编码、仓库编码、SKU编码、排名、实发数量、数量占比（%）；"
        "第二张标题为各仓SKU全集，完整列出同范围货主编码、仓库编码、SKU编码、实发数量，"
        "不能截断或省略未进入排名的SKU。独立核对两张表中入榜SKU的数量一致后再返回。"
        "不要展示SQL或内部对账明细。"
    )


def complete_rows(client, table):
    rows = list(table["rows"])
    paging = table.get("pagination")
    if paging:
        assert paging["reconciled"] is True, "unreconciled_pages"
        assert 1 <= paging["page_count"] <= 1000, "invalid_page_count"
        for number in range(2, paging["page_count"] + 1):
            page = client.request(f"/v1/results/{paging['result_id']}/pages/{number}")["business_table"]
            assert page["columns"] == table["columns"], "page_projection_changed"
            assert page["pagination"]["total_rows"] == paging["total_rows"], "page_total_changed"
            rows.extend(page["rows"])
        assert len(rows) == paging["total_rows"], "missing_page_rows"
    return rows


def records(table, rows, labels):
    columns = {_label(c["label"]): c["key"] for c in table["columns"]}
    assert len(columns) == len(table["columns"]), "duplicate_column"
    assert set(map(_label, labels)) == set(columns), "incorrect_business_columns"
    return [[row[columns[_label(label)]] for label in labels] for row in rows]


def quantities(rows):
    values = {}
    for owner, warehouse, sku, quantity in rows:
        identity = tuple(str(v).strip() if v is not None else "" for v in (owner, warehouse, sku))
        assert all(identity) and identity not in values, "duplicate_or_missing_source_identity"
        values[identity] = _number(quantity)
    return values


def verify_reference(client, task, reference):
    assert reference["state"] == "completed" and reference["http_status"] == 200, "reference_incomplete"
    assert reference["result"].get("result_state") == "live_success", "reference_not_live"
    table = reference["result"]["business_table"]
    keys = ("cargo_owner", "warehouse_code", "sku_code", "quantity")
    assert set(keys) <= {c["key"] for c in table["columns"]}, "reference_not_reviewed_projection"
    baseline = quantities([[row[k] for k in keys] for row in complete_rows(client, table)])
    source = next(t for t in task["result"]["business_tables"] if t["title"] == "各仓SKU全集")
    observed = quantities(records(source, complete_rows(client, source), ["货主编码", "仓库编码", "SKU编码", "实发数量"]))
    assert observed == baseline, "reviewed_business_reference_mismatch"
    return len(baseline)


def reference_question(day):
    return f"{day}所有货主所有仓库的成品实际销售出库，按货主和仓库和SKU汇总"


def verify(client, task, top):
    result = task.get("result") or {}
    assert task["state"] == "completed" and task["http_status"] == 200, "task_incomplete"
    assert result.get("result_state") == "live_success", "not_live_data"
    tables = result.get("business_tables")
    assert isinstance(tables, list) and len(tables) == 2, "missing_requested_table"
    by_title = {t["title"]: t for t in tables}
    assert set(by_title) == {"各仓排名", "各仓SKU全集"}, "incorrect_table_identity"
    ranking, full = by_title["各仓排名"], by_title["各仓SKU全集"]
    all_rows = complete_rows(client, full)
    rank_rows = complete_rows(client, ranking)
    dimensions = ["货主编码", "仓库编码", "SKU编码"]
    source = records(full, all_rows, [*dimensions, "实发数量"])
    output = records(ranking, rank_rows, [*dimensions, "排名", "实发数量", "数量占比"])
    groups = defaultdict(list)
    for identity, quantity in quantities(source).items():
        groups[identity[:2]].append((identity[2], quantity))
    assert groups, "empty_source"
    expected = {}
    for group, items in groups.items():
        total = sum(q for _, q in items)
        for rank, (sku, quantity) in enumerate(sorted(items, key=lambda item: (-item[1], item[0]))[:top], 1):
            expected[(*group, sku)] = (rank, quantity, total)
    seen = set()
    for owner, warehouse, sku, rank, quantity, share in output:
        identity = tuple(str(v).strip() if v is not None else "" for v in (owner, warehouse, sku))
        assert identity in expected and identity not in seen, "incorrect_rank_membership"
        seen.add(identity)
        expected_rank, expected_quantity, total = expected[identity]
        assert _number(rank) == expected_rank, "incorrect_partition_rank"
        assert _number(quantity) == expected_quantity, "ranking_quantity_changed"
        if total == 0:
            assert share == "分母为零", "incorrect_zero_denominator"
        else:
            assert abs(_number(share) - expected_quantity / total * 100) <= Decimal("0.005"), "incorrect_preslice_share"
    assert seen == set(expected), "missing_partition_or_rank"
    wire = json.dumps([result.get("business_result"), *all_rows, *rank_rows], ensure_ascii=False).casefold()
    assert not any(marker in wire for marker in ("ywzt_ods.", "select ", "traceback (", "password=")), "internal_details_exposed"
    return {"groups": len(groups), "source_rows": len(source), "ranking_rows": len(output)}


def submit(client, session, text):
    task = client.request("/v1/tasks", {"conversation_id": session["conversation_id"], "question": text},
                          session["csrf_token"], "partition-" + uuid4().hex)
    started = time.monotonic()
    while task["state"] in {"queued", "running"} and time.monotonic() - started < 600:
        time.sleep(2)
        task = client.request("/v1/tasks/" + task["task_id"])
    return task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, choices=range(1, 21), default=3)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--task-id", help="verify an already completed task without repeating its model query")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"case": "partition_ranking", "top": args.top, "state": "not_run"}))
        return
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    started = time.monotonic()
    task = client.request("/v1/tasks/" + args.task_id) if args.task_id else submit(client, session, question(args.top))
    counts = verify(client, task, args.top)
    day = datetime.fromtimestamp(task["created_at"], ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    reference = submit(client, session, reference_question(day.isoformat()))
    counts["reviewed_reference_rows"] = verify_reference(client, task, reference)
    print(json.dumps({"case": "partition_ranking", "top": args.top, "task_id": task["task_id"],
                      "state": task["state"], **counts, "elapsed_seconds": round(time.monotonic() - started, 2)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
