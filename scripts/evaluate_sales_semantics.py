"""Opt-in semantic acceptance through chat plus independent Doris references.

Only states and boolean checks leave memory. Never store source or public rows.
The reference is deliberately independent of the production sales compiler.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import json
import re
from pathlib import Path
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_generalization import ChatClient, _label


@dataclass(frozen=True)
class SalesCase:
    id: str
    question: str
    start: datetime
    end: datetime
    grain: str
    group_labels: tuple[str, ...]
    exceptions: bool = False
    check_zero: bool = False
    check_skus: bool = False
    order_types: tuple[str, ...] = ()


def cases(today):
    end = datetime.combine(today, datetime.min.time())
    start = end - timedelta(days=1)
    return [
        SalesCase("status-zero-documents",
            f"查询{start:%Y-%m-%d}全天所有货主的实际销售出库，按出库状态统计实发数量和出库单数；"
            "同时列出各状态实发总量为0的出库单数。有效状态700、900、701、901，零数量单据不能丢弃。用表格展示。",
            start, end, "shipment_status", ("出库状态", "状态"), check_zero=True),
        SalesCase("finished-goods-exception-union",
            f"查询{start:%Y-%m-%d}全天所有货主的成品销售出库，成品定义为 master_sku.sku_group = '50'，"
            "同时保留 SKU 5101010010008 和 5101010010009 两个例外。按 SKU 汇总实际出库量和出库单数，用表格展示。",
            start, end, "sku", ("SKU", "SKU编码", "商品编码"), exceptions=True),
        SalesCase("exclusive-midnight-daily",
            "统计2026-09-01至2026-09-10 0点前的每日实际销售出库数量和出库单数，按日期汇总，用表格展示。",
            datetime(2026, 9, 1), datetime(2026, 9, 10), "ship_date", ("日期", "出库日期")),
        SalesCase("intraday-order-types",
            "查询北京时间2026-09-01 08:30:00至2026-09-02 12:00:00（不含结束时点）所有货主实际销售出库，"
            "按出库类型统计实发数量、出库单数和SKU数。不限制成品或仓库，用表格展示。",
            datetime(2026, 9, 1, 8, 30), datetime(2026, 9, 2, 12), "ref_order_type", ("出库类型", "来源单据类型", "来源订单类型"), check_skus=True),
        SalesCase("explicit-type-union",
            "2026-09-01全天JYCK、HHCK、BFCK、QTCK实际销售出库数量和出库单数，按出库类型汇总，用表格展示。",
            datetime(2026, 9, 1), datetime(2026, 9, 2), "ref_order_type", ("出库类型", "来源单据类型"),
            order_types=("JYCK", "HHCK", "BFCK", "QTCK")),
        SalesCase("reviewed-type-union",
            "昨天JYCK和HHCK实际销售出库，出库类型汇总",
            start, end, "ref_order_type", ("出库类型", "来源单据类型"),
            order_types=("JYCK", "HHCK")),
    ]


def validate_reference_authority(metric, exception_metric=None):
    filters = metric.get("filters", {})
    if (metric.get("id") != "sales.outbound_actual" or str(metric.get("version")) != "2.0.0"
            or metric.get("status") != "active" or metric.get("approval", {}).get("state") != "approved"
            or metric.get("unit") != "EA" or metric.get("formula") != "SUM(d.ship_qty)"
            or metric.get("time_window", {}).get("field") != "h.ship_at"
            or set(filters.get("completion_statuses", [])) != {700, 900, 701, 901}
            or set(filters.get("valid_source_order_types", [])) != {"JYCK", "HHCK", "BFCK", "QTCK", "SO%"}
            or filters.get("shipment_header_key") != ["company_code", "outbound_code"]):
        raise ValueError("reference authority changed; review before verification")
    if exception_metric is not None and (
            exception_metric.get("id") != "sales.finished_goods_exceptions"
            or str(exception_metric.get("version")) != "1.0.0" or exception_metric.get("status") != "active"
            or exception_metric.get("approval", {}).get("state") != "approved"
            or exception_metric.get("filters", {}).get("finished_group") != "50"
            or set(exception_metric.get("filters", {}).get("exception_skus", [])) != {"5101010010008", "5101010010009"}):
        raise ValueError("exception reference authority changed")


def independent_sql(case):
    if (not isinstance(case.start, datetime) or not isinstance(case.end, datetime)
            or case.start.tzinfo is not None or case.end.tzinfo is not None or case.end <= case.start):
        raise ValueError("reference requires explicit local datetime boundaries")
    group = {"shipment_status": "h.shipment_status", "sku": "TRIM(d.sku_code)",
             "ship_date": "CAST(DATE(h.ship_at) AS STRING)", "ref_order_type": "h.ref_order_type"}[case.grain]
    if len(case.order_types) != len(set(case.order_types)) or any(
            not re.fullmatch(r"JYCK|HHCK|BFCK|QTCK|SO[0-9]+", value) for value in case.order_types):
        raise ValueError("invalid reference type set")
    type_filter = "AND TRIM(h.ref_order_type) IN (" + ",".join("'" + value + "'" for value in case.order_types) + ")" if case.order_types else ""
    membership = """AND (TRIM(d.sku_code) IN ('5101010010008', '5101010010009') OR EXISTS (
        SELECT 1 FROM ywzt_ods.master_sku s
        WHERE TRIM(s.sku_code)=TRIM(d.sku_code) AND TRIM(CAST(s.sku_group AS STRING))='50'))""" if case.exceptions else ""
    return f"""WITH header_versions AS (
 SELECT DISTINCT TRIM(h.company_code) AS owner, TRIM(h.outbound_code) AS document,
  h.ship_at, TRIM(CAST(h.status AS STRING)) AS shipment_status,
  TRIM(h.facility_code) AS warehouse, TRIM(h.ref_order_type) AS ref_order_type,
  TRIM(h.ref_order_code) AS source_order, TRIM(h.terms_of_shipment) AS fulfillment
 FROM ywzt_ods.scm_sales_shipment_header h
 WHERE h.ship_at >= '{case.start:%Y-%m-%d %H:%M:%S}' AND h.ship_at < '{case.end:%Y-%m-%d %H:%M:%S}'
  AND h.status IN (700,900,701,901)
  AND (TRIM(h.ref_order_type) IN ('JYCK','HHCK','BFCK','QTCK') OR TRIM(h.ref_order_type) LIKE 'SO%')
  {type_filter}
  AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
  AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header o
   WHERE TRIM(o.company_code)=TRIM(h.company_code) AND TRIM(o.sales_order_code)=TRIM(h.ref_order_code)
    AND TRIM(o.sales_order_code)<>'' AND COALESCE(o.deleted_flag,0)=0 AND IFNULL(o._fdl_marked_deleted,'false')='false')
), headers AS (
 SELECT owner,document,MIN(ship_at) AS ship_at,MIN(shipment_status) AS shipment_status,
  MIN(ref_order_type) AS ref_order_type,COUNT(*) AS versions
 FROM header_versions GROUP BY owner,document
), detail AS (
 SELECT {group} AS group_key,h.owner,h.document,TRIM(d.sku_code) AS sku_code,
  d.ship_qty AS quantity,h.versions
 FROM headers h JOIN ywzt_ods.scm_sales_shipment_detail d
  ON TRIM(d.company_code)=h.owner AND TRIM(d.outbound_code)=h.document
 WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
 {membership}
), documents AS (
 SELECT group_key,owner,document,SUM(quantity) AS quantity
 FROM detail GROUP BY group_key,owner,document
), summary AS (
 SELECT group_key,SUM(quantity) AS quantity,COUNT(*) AS outbound_count,
  SUM(CASE WHEN quantity=0 THEN 1 ELSE 0 END) AS zero_documents
 FROM documents GROUP BY group_key
), controls AS (
 SELECT SUM(CASE WHEN quantity IS NULL OR quantity<0 OR versions<>1
   OR COALESCE(owner,'')='' OR COALESCE(document,'')='' OR COALESCE(sku_code,'')='' THEN 1 ELSE 0 END) AS invalid_rows
 FROM detail
), sku_counts AS (
 SELECT group_key,COUNT(DISTINCT sku_code) AS sku_count FROM detail GROUP BY group_key
)
SELECT s.*,k.sku_count,c.invalid_rows FROM summary s
JOIN sku_counts k ON k.group_key=s.group_key CROSS JOIN controls c
ORDER BY s.group_key LIMIT 1001"""


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing number")
    value = Decimal(str(value).replace(",", ""))
    if not value.is_finite():
        raise ValueError("nonfinite number")
    return value


def reference_values(rows):
    if len(rows) > 1000:
        raise ValueError("reference is not complete")
    result = {}
    for row in rows:
        key = str(row["group_key"])
        if not key or key in result or number(row["invalid_rows"]) != 0:
            raise ValueError("reference quality failed")
        values = tuple(number(row[name]) for name in ("quantity", "outbound_count", "zero_documents", "sku_count"))
        if any(v < 0 for v in values) or any(v != v.to_integral_value() for v in values[1:]):
            raise ValueError("reference quantities invalid")
        result[key] = values
    return result


def compare(case, result, table, reference):
    labels = [(case.group_labels), ("实发数量", "实发量", "实际销售出库量", "实际出库量", "实际销售出库", "出库数量", "数量"), ("出库单数",)]
    if case.check_zero:
        labels.append(("零数量出库单数", "零实发出库单数", "零数量单据数", "零实发单数"))
    if case.check_skus:
        labels.append(("SKU数", "SKU数量"))
    keys = []
    for aliases in labels:
        matching = [c["key"] for c in table["columns"] if _label(c["label"]) in set(map(_label, aliases))]
        if len(matching) != 1:
            return {"required_projection": False}
        keys.append(matching[0])
    expected = reference_values(reference)
    actual = {}
    for row in table["rows"]:
        key = str(row[keys[0]])
        if key in actual:
            return {"unique_grain": False}
        actual[key] = tuple(number(row[k]) for k in keys[1:])
    measures = (0, 1) + ((2,) if case.check_zero else ()) + ((3,) if case.check_skus else ())
    correct = {key: tuple(values[i] for i in measures) for key, values in expected.items()}
    wire = json.dumps(result, ensure_ascii=False).casefold()
    return {"complete_live_state": result.get("result_state") == ("live_success" if expected else "no_data"),
            "all_groups_and_values_match": actual == correct,
            "unique_grain": len(actual) == len(table["rows"]),
            "no_private_details": not any(s in wire for s in ("ywzt_ods.", "select ", "password=", "traceback ("))}


def complete_table(client, table):
    page = table.get("pagination")
    if not page:
        return table
    if page.get("page") != 1 or not 1 <= page["page_count"] <= 100 or page["total_rows"] > 1000:
        raise ValueError("unbounded acceptance pages")
    rows = list(table["rows"])
    for index in range(2, page["page_count"] + 1):
        next_table = client.request(f"/v1/results/{page['result_id']}/pages/{index}")["business_table"]
        metadata = next_table["pagination"]
        if (next_table["columns"] != table["columns"] or next_table["title"] != table["title"]
                or metadata["result_id"] != page["result_id"] or metadata["page"] != index
                or metadata["total_rows"] != page["total_rows"] or metadata["start_row"] != len(rows) + 1):
            raise ValueError("inconsistent acceptance page")
        rows.extend(next_table["rows"])
    if len(rows) != page["total_rows"]:
        raise ValueError("incomplete acceptance pages")
    return {**table, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    selected = cases(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    if set(args.case) - {c.id for c in selected}:
        parser.error("unknown case")
    if args.case:
        selected = [c for c in selected if c.id in args.case]
    if not args.live:
        for case in selected:
            print(json.dumps({"case_id": case.id, "state": "not_run"}))
        return 0
    from app.codex_agent import _hide_windows_subprocesses
    from app.codex_mcp_bridge import CodexMcpBridge
    from app.query_outcomes import validate_doris_result
    from app.knowledge_activation import active_snapshot
    import yaml

    _hide_windows_subprocesses()
    contract_root, _ = active_snapshot(ROOT)
    metric = yaml.safe_load((contract_root / "metrics/sales.outbound_actual.v2.yaml").read_text(encoding="utf-8"))
    exceptions = yaml.safe_load((contract_root / "metrics/sales.finished_goods_exceptions.yaml").read_text(encoding="utf-8")) if any(c.exceptions for c in selected) else None
    validate_reference_authority(metric, exceptions)
    client = ChatClient("http://127.0.0.1:8021")
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    for case in selected:
        sql = independent_sql(case)
        before = validate_doris_result(bridge.query(sql=sql, purpose="Independent sales acceptance before")).rows
        baseline = reference_values(before)
        started = time.monotonic()
        session = client.request("/v1/session")
        if session.get("clarification_pending"):
            raise ValueError("existing clarification must remain untouched")
        task = client.request("/v1/tasks", {"question": case.question, "conversation_id": session["conversation_id"]},
            session["csrf_token"], "sales-semantic-" + uuid4().hex)
        print(json.dumps({"case_id": case.id, "task_id": task["task_id"], "state": task["state"]}), flush=True)
        while task["state"] in {"queued", "running"} and time.monotonic() - started < 960:
            time.sleep(2)
            task = client.request("/v1/tasks/" + task["task_id"])
        result = task.get("result") or {}
        if task["state"] != "completed" or not isinstance(result.get("business_table"), dict):
            print(json.dumps({"case_id": case.id, "state": task["state"], "result_state": result.get("result_state"),
                              "error_code": result.get("error", {}).get("code")}), flush=True)
            return 1
        table = complete_table(client, result["business_table"])
        after = validate_doris_result(bridge.query(sql=sql, purpose="Independent sales acceptance after")).rows
        checks = compare(case, result, table, before)
        checks["stable_source_reference"] = baseline == reference_values(after)
        print(json.dumps({"case_id": case.id, "state": task["state"], "elapsed_seconds": round(time.monotonic() - started, 2), "checks": checks}), flush=True)
        if not all(checks.values()):
            return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"state": "acceptance_failed", "category": type(exc).__name__,
                          "reason": getattr(exc, "reason", "acceptance_failed")}), flush=True)
        raise SystemExit(2) from None
