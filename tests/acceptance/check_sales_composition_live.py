"""Verify compound sales requests through chat; print no business values."""

from decimal import Decimal
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import sqlglot

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_generalization import ChatClient
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from tests.acceptance.check_outbound_boxes_live import reference_sql as ledger_reference_sql


CASES = (
    ("type_sku_count", "昨天各仓库JYCK实际销售出库数量和SKU数", False, None),
    ("channel_share_sku_count", "昨天实际销售出库，按仓库和渠道汇总，计算数量占比，返回SKU数", True, None),
    ("channel_daily_sku_count", "近14天实际销售出库，按渠道和仓库汇总，日均量，SKU数", False, 14),
    ("chinese_duration_customer_channel", "近十四个完整自然日实际销售出库，按客户渠道和仓库汇总，日均量，SKU数", False, 14),
    ("customer_channel_share", "昨天实际销售出库，按仓库和客户渠道汇总，计算数量占比，返回SKU数", True, None),
)


def independent_reference_sql(case, start, end):
    """Aggregate the independent source ledger, never the production compiler."""
    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("invalid reference interval")
    channel = bool(case[2] or case[3])
    tree = sqlglot.parse_one(ledger_reference_sql(
        start, end, all_actual=case[0] != "type_sku_count", finished_goods=False,
    ), read="doris")
    ctes = [cte.sql(dialect="doris") for cte in tree.args["with_"].expressions
            if cte.alias_or_name in {"distinct_headers", "customer_types", "raw_facts", "facts"}]
    dimensions = "owner,warehouse" + (",channel" if channel else "")
    return "WITH " + ",".join(ctes) + f""", expected AS (
 SELECT {dimensions}, SUM(quantity) quantity, COUNT(DISTINCT doc) document_count,
  COUNT(DISTINCT sku) sku_count FROM facts GROUP BY {dimensions}
), documents AS (SELECT DISTINCT owner,doc FROM facts)
SELECT e.*, (SELECT COUNT(*) FROM expected) group_count,
 (SELECT COALESCE(SUM(quantity),0) FROM facts) total_quantity,
 (SELECT COUNT(*) FROM documents) total_documents,
 (SELECT COUNT(DISTINCT sku) FROM facts) total_skus
FROM expected e ORDER BY {dimensions} LIMIT 100001"""


def verify_reference(case, rows, table, reference):
    units = {column["key"]: column.get("unit") for column in table["columns"]}
    required_units = {"quantity": "EA", "outbound_count": "单", "sku_count": "个"}
    if case[3]:
        required_units["daily_average"] = "EA/日"
    assert all(units.get(key) == unit for key, unit in required_units.items()), "public_unit_mismatch"
    expected, actual = {}, {}
    channel = bool(case[2] or case[3])
    fields = ("owner", "warehouse") + (("channel",) if channel else ())
    public_fields = ("cargo_owner", "warehouse_code") + (("customer_channel",) if channel else ())
    controls = None
    for row in reference:
        key = tuple(str(row[field]) for field in fields)
        assert key not in expected, "duplicate_independent_group"
        expected[key] = tuple(Decimal(str(row[field])) for field in ("quantity", "document_count", "sku_count"))
        current = tuple(Decimal(str(row[field])) for field in ("group_count", "total_quantity", "total_documents", "total_skus"))
        assert controls is None or controls == current, "independent_controls_changed"
        controls = current
    assert controls is not None and controls[0] == len(reference), "independent_reference_incomplete"
    for row in rows:
        key = tuple(str(row[field]) for field in public_fields)
        assert key not in actual, "duplicate_public_group"
        actual[key] = tuple(Decimal(str(row[field])) for field in ("quantity", "outbound_count", "sku_count"))
    assert actual == expected, "independent_group_measures_mismatch"
    summary = {item["label"]: Decimal(item["value"]) for item in table["summary"]}
    assert (summary["实际销售出库"], summary["出库单数"], summary["SKU数"]) == controls[1:], "independent_totals_mismatch"


def verify(case, client, task, reference=None):
    identifier, _, share, days = case
    result = task.get("result") or {}
    assert task["state"] == "completed" and task["http_status"] == 200, "task_incomplete"
    assert result.get("result_state") == "live_success", "not_live_data"
    table = result.get("business_table")
    assert isinstance(table, dict), "missing_table"
    keys = [c["key"] for c in table["columns"]]
    required = {"cargo_owner", "warehouse_code", "warehouse_name", "quantity", "outbound_count", "sku_count"}
    if share or days:
        required.add("customer_channel")
    if share:
        required.add("quantity_share")
    if days:
        required.add("daily_average")
    assert required <= set(keys), "requested_column_missing"
    rows = list(table["rows"])
    paging = table.get("pagination")
    if paging:
        assert paging["reconciled"] is True, "unreconciled_pages"
        assert 1 <= paging["page_count"] <= 1000, "invalid_page_count"
        for number in range(2, paging["page_count"] + 1):
            page = client.request(f"/v1/results/{paging['result_id']}/pages/{number}")["business_table"]
            assert page["columns"] == table["columns"], "page_columns_changed"
            assert page["summary"] == table["summary"], "page_summary_changed"
            assert page["pagination"]["total_rows"] == paging["total_rows"], "page_count_changed"
            rows.extend(page["rows"])
        assert len(rows) == paging["total_rows"], "incomplete_pagination"
    summary = {v["label"]: Decimal(v["value"]) for v in table["summary"]}
    total = summary["实际销售出库"]
    assert sum(Decimal(r["quantity"]) for r in rows) == total, "quantity_total_mismatch"
    assert sum(Decimal(r["outbound_count"]) for r in rows) == summary["出库单数"], "document_total_mismatch"
    groups = [(r["cargo_owner"], r["warehouse_code"], r.get("customer_channel")) for r in rows]
    assert len(groups) == len(set(groups)), "duplicate_group"
    counts = [Decimal(r["sku_count"]) for r in rows]
    assert counts and all(c >= 1 and c == c.to_integral_value() for c in counts), "invalid_sku_count"
    assert max(counts) <= summary["SKU数"] <= sum(counts), "invalid_sku_total"
    for row in rows:
        quantity = Decimal(row["quantity"])
        if share:
            if total == 0:
                assert row["quantity_share"] == "分母为零", "invalid_zero_denominator"
            else:
                assert abs(Decimal(row["quantity_share"].removesuffix("%")) - quantity / total * 100) <= Decimal("0.005"), "invalid_share"
        if days:
            assert abs(Decimal(row["daily_average"]) - quantity / days) <= Decimal("0.005"), "invalid_daily_average"
    notes = "".join(table.get("notes", []))
    assert "SKU数" in notes and "数据同步时间" in notes, "business_notes_missing"
    if identifier == "type_sku_count":
        assert "JYCK" in notes, "type_scope_missing"
    wire = json.dumps({**table, "rows": rows}, ensure_ascii=False).casefold()
    assert not any(v in wire for v in ("ywzt_ods.", "select ", "traceback (", "password=")), "internal_details_exposed"
    if reference is not None:
        verify_reference(case, rows, table, reference)
    return len(rows), len(keys), paging["page_count"] if paging else 1


def main():
    _hide_windows_subprocesses()
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    for case in CASES:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        sql = independent_reference_sql(case, today - timedelta(days=case[3] or 1), today)
        before = validate_doris_result(bridge.query(sql=sql, purpose="independent composed-sales source controls")).rows
        started = time.monotonic()
        task = client.request("/v1/tasks", {"conversation_id": session["conversation_id"], "question": case[1]},
                              session["csrf_token"], "composition-" + uuid4().hex)
        while task["state"] in {"queued", "running"} and time.monotonic() - started < 600:
            time.sleep(2)
            task = client.request("/v1/tasks/" + task["task_id"])
        rows, columns, pages = verify(case, client, task, reference=before)
        after = validate_doris_result(bridge.query(sql=sql, purpose="composed-sales source stability")).rows
        assert before == after, "independent_source_changed"
        assert datetime.now(ZoneInfo("Asia/Shanghai")).date() == today, "calendar_anchor_changed"
        print(json.dumps({"case": case[0], "task_id": task["task_id"], "state": task["state"],
                          "rows": rows, "columns": columns, "pages": pages,
                          "independent_source_match": True,
                          "elapsed_seconds": round(time.monotonic() - started, 2)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Only assertions authored above are safe to print, never API payloads.
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None
