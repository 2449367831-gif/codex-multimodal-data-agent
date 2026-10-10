"""Model-only inventory composition checked against an independent ledger."""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.planning import RequestPlanner
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient, _number
from tests.acceptance.check_partition_ranking_live import complete_rows, records


LABELS = ["货主编码", "可用库存", "在途库存", "成品库存", "在途占比", "库存占比"]
QUESTION = (
    "按已发布的当前实时可用成品库存加在途口径，查询全部货主、全部仓库、全部成品SKU，"
    "不限制渠道，不沿用上一问的日期、范围或条件。按货主编码汇总，保留全部有成品库存源记录的货主，"
    "零数量也保留。分别给出可用库存、在途库存和两者之和作为成品库存；"
    "在途占比等于该货主在途库存除以该货主成品库存，库存占比等于该货主成品库存除以全部货主成品库存。"
    "两种占比均用百分比、保留两位小数，分母为零时显示空值，不合并或猜测货主编码。"
    "成品库存按来源中的有符号数量先汇总，不能去掉负数源记录或把重复库存事实去重。"
    "按成品库存降序、货主编码升序，完整返回货主编码、可用库存、在途库存、成品库存、在途占比、库存占比六列，"
    "编码为文本，数量单位EA。标明当前状态和来源鲜度，不展示查询语句或内部核对明细。"
)


def ledger_sql():
    # EXISTS avoids multiplying inventory facts by repeated SKU master rows.
    # Individual components are summed independently before the Python formula.
    return """WITH warehouse_components AS (
SELECT TRIM(i.owner) AS owner,TRIM(i.wms_desc) AS warehouse,
 SUM(COALESCE(i.available_quantity,0)) AS available,
 SUM(COALESCE(i.in_transit_quantity,0)) AS transit,
 COUNT(*) AS fact_count,
 SUM(CASE WHEN i.owner IS NULL OR TRIM(i.owner)='' THEN 1 ELSE 0 END) AS invalid_owner
FROM ywzt_ods.sku_inventory i
WHERE EXISTS (SELECT 1 FROM ywzt_ods.master_sku s
 WHERE TRIM(s.sku_code)=TRIM(i.sku_code) AND TRIM(s.sku_group)='50')
GROUP BY TRIM(i.owner),TRIM(i.wms_desc)
)
SELECT owner,SUM(available) AS available,SUM(transit) AS transit,
 SUM(fact_count) AS fact_count,SUM(invalid_owner) AS invalid_owner,
 SUM(CASE WHEN available+transit<0 THEN 1 ELSE 0 END) AS negative_warehouse_count
FROM warehouse_components GROUP BY owner ORDER BY owner"""


def expected(rows):
    owners = {}
    for row in rows:
        owner = row["owner"]
        assert isinstance(owner, str) and owner and owner == owner.strip(), "invalid_source_owner"
        assert owner not in owners, "duplicate_source_owner"
        assert _number(row["invalid_owner"]) == 0, "invalid_source_owner"
        assert _number(row["fact_count"]) > 0, "missing_inventory_facts"
        available, transit = _number(row["available"]), _number(row["transit"])
        assert available + transit >= 0, "negative_owner_inventory"
        owners[owner] = (available, transit)
    total = sum((a + t for a, t in owners.values()), Decimal(0))
    output = []
    for owner, (available, transit) in owners.items():
        quantity = available + transit
        output.append((owner, available, transit, quantity,
                       transit / quantity * 100 if quantity else None,
                       quantity / total * 100 if total else None))
    return sorted(output, key=lambda row: (-row[3], row[0]))


def verify_quality_response(result, invalid_net):
    assert invalid_net, "no_independent_inventory_quality_failure"
    assert result.get("error", {}).get("code") == "inventory_quality_failed", "missing_business_failure_cause"
    assert not result.get("business_table") and not result.get("business_tables"), "invalid_inventory_published"


def verify(table, rows, baseline):
    assert table.get("table_id") == "model_verified", "not_model_execution"
    output = records(table, rows, LABELS)
    assert len(output) == len(baseline), "incorrect_owner_population"
    columns = {column["label"]: column for column in table["columns"]}
    assert columns[LABELS[0]]["kind"] == "text", "owner_identity_type"
    for label, unit in zip(LABELS[1:], ("EA", "EA", "EA", "%", "%")):
        assert columns[label]["kind"] == "number" and columns[label].get("unit") == unit, "quantity_type_or_unit"
    for actual, reference in zip(output, baseline):
        assert actual[0] == reference[0], "owner_identity_or_sort"
        assert tuple(_number(value) for value in actual[1:4]) == reference[1:4], "inventory_components_mismatch"
        for observed, wanted in zip(actual[4:], reference[4:]):
            assert (observed is None if wanted is None else
                    abs(_number(observed) - wanted) <= Decimal("0.005")), "inventory_share_mismatch"
    wire = json.dumps(table, ensure_ascii=False).casefold()
    assert not any(x in wire for x in ("select ", "ywzt_ods.", "traceback (", "password=")), "internal_disclosure"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    reviewed_request = RequestPlanner().classify(QUESTION)
    assert reviewed_request is not None
    assert reviewed_request.route == "current_inventory_owner_summary"
    if not args.live:
        print(json.dumps({"case": "model_inventory_shares", "state": "not_run", "fixed_route": False}))
        return
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    def baseline():
        rows = validate_doris_result(bridge.query(
            sql=ledger_sql(), purpose="model inventory holdout independent baseline")).rows
        return expected(rows), any(_number(row["negative_warehouse_count"]) > 0 for row in rows)
    before = baseline()
    started = time.monotonic()
    task = client.request("/v1/tasks/" + args.task_id) if args.task_id else client.request(
        "/v1/tasks", {"conversation_id": session["conversation_id"], "question": QUESTION},
        session["csrf_token"], "model-inventory-holdout-" + uuid4().hex)
    print(json.dumps({"case": "model_inventory_shares", "task_id": task["task_id"], "state": task["state"]}), flush=True)
    while task["state"] in {"queued", "running"} and time.monotonic() - started < 600:
        time.sleep(2)
        task = client.request("/v1/tasks/" + task["task_id"])
    print(json.dumps({"case": "model_inventory_shares", "task_id": task["task_id"], "state": task["state"],
                      "http_status": task.get("http_status"), "elapsed_seconds": round(time.monotonic()-started, 2)}), flush=True)
    assert before == baseline(), "live_inventory_changed_during_verification"
    if before[1]:
        assert task["state"] == "failed" and task.get("http_status") == 502, "invalid_inventory_not_rejected"
        verify_quality_response(task["result"], before[1])
        print(json.dumps({"case": "inventory_owner_summary", "quality_rejection_verified": True,
                          "business_acceptance": False, "fixed_route": True}), flush=True)
        return
    assert task["state"] == "completed" and task.get("http_status") == 200, "task_incomplete"
    assert task["result"].get("result_state") in {"live_success", "no_data"}, "no_live_result"
    table = task["result"].get("business_table")
    assert isinstance(table, dict), "missing_table"
    verify(table, complete_rows(client, table), before[0])
    print(json.dumps({"case": "inventory_owner_summary", "verification": "passed", "fixed_route": True}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
