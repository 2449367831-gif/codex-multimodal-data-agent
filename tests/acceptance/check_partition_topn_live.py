"""Independently reconcile an existing per-warehouse Top-N task in memory."""

import argparse
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import sys

import sqlglot

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient
from tests.acceptance.check_outbound_boxes_live import reference_sql
from tests.acceptance.check_partition_ranking_live import complete_rows


def independent_sql(start, end, top_n):
    if type(start) is not date or type(end) is not date or start >= end or type(top_n) is not int or not 1 <= top_n <= 100:
        raise ValueError("invalid reference parameters")
    tree = sqlglot.parse_one(reference_sql(start, end, all_actual=True, finished_goods=False), read="doris")
    ctes = [cte.sql(dialect="doris") for cte in tree.args["with_"].expressions
            if cte.alias_or_name in {"distinct_headers", "customer_types", "raw_facts", "facts"}]
    return "WITH " + ",".join(ctes) + f""", sku_totals AS (
 SELECT owner,warehouse,sku,SUM(quantity) quantity,COUNT(DISTINCT doc) documents
 FROM facts GROUP BY owner,warehouse,sku
), ranked AS (
 SELECT *,ROW_NUMBER() OVER (PARTITION BY owner,warehouse ORDER BY quantity DESC,sku ASC) ranking
 FROM sku_totals
), expected AS (SELECT * FROM ranked WHERE ranking <= {top_n}), quality AS (
 SELECT COUNT(*) - COUNT(quantity) missing_quantity,
  COALESCE(SUM(CASE WHEN quantity<0 THEN 1 ELSE 0 END),0) negative_quantity,
  COALESCE(SUM(CASE WHEN COALESCE(owner,'')='' OR COALESCE(warehouse,'')='' OR COALESCE(sku,'')='' THEN 1 ELSE 0 END),0) missing_key
 FROM facts
)
SELECT e.*,q.*,(SELECT COUNT(*) FROM expected) expected_rows
FROM expected e CROSS JOIN quality q ORDER BY owner,warehouse,ranking LIMIT 10001"""


def compare(table, rows, reference, top_n):
    expected = {}
    for row in reference:
        assert int(row["expected_rows"]) == len(reference), "incomplete_reference"
        assert all(Decimal(str(row[k])) == 0 for k in ("missing_quantity", "negative_quantity", "missing_key")), "reference_quality_failed"
        key = tuple(str(row[k]) for k in ("owner", "warehouse", "sku"))
        assert key not in expected, "duplicate_reference_group"
        expected[key] = (Decimal(str(row["ranking"])), Decimal(str(row["quantity"])), Decimal(str(row["documents"])))
    aliases = (
        ("货主编码", "货主"), ("仓库编码",), ("SKU编码", "SKU", "商品编码"),
        ("仓内排名", "仓库内排名", "排名"), ("实发数量", "实际销售出库", "出库数量"), ("出库单数",),
    )
    keys = []
    for labels in aliases:
        matches = [c for c in table["columns"] if c["label"] in labels]
        assert len(matches) == 1, "missing_or_ambiguous_projection"
        keys.append(matches[0]["key"])
    descriptors = {c["key"]: c for c in table["columns"]}
    assert descriptors[keys[4]].get("unit") == "EA", "quantity_unit_mismatch"
    assert descriptors[keys[5]].get("unit") == "单", "document_unit_mismatch"
    actual, groups = {}, {}
    for row in rows:
        key = tuple(str(row[k]) for k in keys[:3])
        assert key not in actual, "duplicate_public_group"
        values = tuple(Decimal(str(row[k])) for k in keys[3:])
        assert all(v.is_finite() for v in values), "invalid_measure"
        rank, _, documents = values
        assert rank == rank.to_integral_value() and 1 <= rank <= top_n, "invalid_partition_rank"
        assert documents == documents.to_integral_value() and documents >= 0, "invalid_document_count"
        actual[key] = values
        groups.setdefault(key[:2], []).append(int(rank))
    assert all(sorted(ranks) == list(range(1, len(ranks) + 1)) for ranks in groups.values()), "missing_partition_rank"
    assert actual == expected, "independent_partition_mismatch"
    return {"all_partitions": True, "quantities_and_documents": True,
            "complete_ranks": True, "units": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--top-n", type=int, default=3)
    args = parser.parse_args()
    if not re.fullmatch(r"task_[a-f0-9]{32}", args.task_id) or not 1 <= args.days <= 3650:
        parser.error("invalid task or time window")
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    client.request("/v1/session")
    task = client.request("/v1/tasks/" + args.task_id)
    assert task["state"] == "completed", "task_not_completed"
    result = task["result"]
    assert result["result_state"] == "live_success" and not result["clarification"]["required"], "not_live"
    table = result["business_table"]
    sql = independent_sql(args.as_of - timedelta(days=args.days), args.as_of, args.top_n)
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    reference = validate_doris_result(bridge.query(sql=sql, purpose="independent warehouse ranking reference")).rows
    rows = complete_rows(client, table)
    checks = compare(table, rows, reference, args.top_n)
    after = validate_doris_result(bridge.query(sql=sql, purpose="independent ranking source stability")).rows
    assert reference == after, "source_changed"
    checks["source_stability"] = True
    print(json.dumps({"task_id": args.task_id, "verification": "passed", "checks": checks}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
