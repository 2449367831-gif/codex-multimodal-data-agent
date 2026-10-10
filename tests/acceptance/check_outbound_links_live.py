"""Independent T30 relationship reference. Real observations stay in memory."""

from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.outbound_links import OutboundLinkPlan, compile_outbound_links, reconcile_outbound_links
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


def normalized(value):
    return str(value).strip() or None if value is not None else None


def active(row, delete_field=None):
    raw_flag = row.get("_fdl_marked_deleted")
    flag = "false" if raw_flag is None else str(raw_flag).strip().lower()
    delete = row.get(delete_field) if delete_field else 0
    delete = 0 if delete is None else delete
    if str(delete) == "1" or flag == "true":
        return False
    if str(delete) != "0" or flag != "false":
        raise ValueError("reference_unknown_deletion_marker")
    return True


def expected_links(headers, orders, details):
    shipment_map = defaultdict(set)
    order_map = defaultdict(set)
    detail_map = defaultdict(set)
    for row in headers:
        if not active(row, "delete_flag"):
            continue
        key = normalized(row["company_code"]), normalized(row["outbound_code"])
        if not all(key):
            raise ValueError("reference_invalid_shipment_identity")
        stamp = str(row["ship_at"]).replace("T", " ") if row["ship_at"] is not None else None
        shipment_map[key].add((normalized(row["ref_order_code"]), normalized(row["ref_order_type"]), stamp))
    if any(len(values) != 1 for values in shipment_map.values()):
        raise ValueError("reference_conflicting_shipment")
    references = {(owner, next(iter(values))[0]) for (owner, _), values in shipment_map.items()}
    for row in orders:
        key = normalized(row["company_code"]), normalized(row["sales_order_code"])
        if key not in references or not active(row, "deleted_flag"):
            continue
        order_map[key].add(tuple(normalized(row[k]) for k in ("source_order_code", "source_platform_code", "order_type")))
    if any(len(values) != 1 for values in order_map.values()):
        raise ValueError("reference_conflicting_order")
    for row in details:
        key = normalized(row["company_code"]), normalized(row["sales_order_code"])
        if key not in order_map or str(row["status"]) != "0" or not active(row):
            continue
        detail_map[key].add(normalized(row["source_order_code"]))
    result = []
    for (owner, outbound), observations in shipment_map.items():
        ref, ref_type, stamp = next(iter(observations))
        matches = order_map.get((owner, ref))
        source, platform, order_type = next(iter(matches)) if matches else (None, None, None)
        for detail in detail_map.get((owner, ref)) or {None}:
            result.append({"cargo_owner": owner, "outbound_code": outbound,
                           "referenced_order_code": ref, "sales_order_code": ref if matches else None,
                           "source_order_code": source, "detail_source_order_code": detail,
                           "source_platform_code": platform, "ref_order_type": ref_type,
                           "order_type": order_type, "ship_at": stamp,
                           "relationship_state": "matched" if matches else "unmatched"})
    return result


def canonical(rows):
    return sorted(json.dumps({**row, "ship_at": str(row["ship_at"]).replace("T", " ") if row["ship_at"] is not None else None}, sort_keys=True) for row in rows)


def reference_queries(codes):
    predicate = exp.Trim(this=exp.column("outbound_code", table="h")).isin(*[exp.Literal.string(code) for code in codes]).sql(dialect="doris")
    header = f"SELECT h.* FROM ywzt_ods.scm_sales_shipment_header h WHERE {predicate} LIMIT 10000"
    orders = f"""SELECT o.company_code,o.sales_order_code,o.source_order_code,o.source_platform_code,
      o.order_type,o.deleted_flag,o._fdl_marked_deleted
      FROM ywzt_ods.scm_sales_order_header o
      WHERE EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_shipment_header h
        WHERE {predicate} AND TRIM(h.company_code)=TRIM(o.company_code)
        AND TRIM(h.ref_order_code)=TRIM(o.sales_order_code)) LIMIT 10000"""
    details = f"""SELECT d.company_code,d.sales_order_code,d.source_order_code,d.status,d._fdl_marked_deleted
      FROM ywzt_ods.scm_sales_order_detail d
      WHERE EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_shipment_header h
        WHERE {predicate} AND TRIM(h.company_code)=TRIM(d.company_code)
        AND TRIM(h.ref_order_code)=TRIM(d.sales_order_code)) LIMIT 10000"""
    # Only relation fields are needed; never fetch customer contact or address columns.
    header = header.replace("h.*", "h.company_code,h.outbound_code,h.ref_order_code,h.ref_order_type,h.ship_at,h.delete_flag,h._fdl_marked_deleted")
    return header, orders, details


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    def query(sql):
        rows = validate_doris_result(bridge.query(sql=sql, purpose="T30 independent candidate relationship acceptance")).rows
        if len(rows) >= 10000:
            raise ValueError("reference_capacity_exceeded")
        return rows
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    # Deterministic selection is fixed before observing any candidate result.
    sample = query(f"""SELECT DISTINCT TRIM(outbound_code) AS outbound_code
      FROM ywzt_ods.scm_sales_shipment_header
      WHERE ship_at >= '{today - timedelta(days=1)} 00:00:00' AND ship_at < '{today} 00:00:00'
        AND outbound_code IS NOT NULL AND TRIM(outbound_code) <> ''
        AND COALESCE(delete_flag,0)=0 AND COALESCE(_fdl_marked_deleted,'false')='false'
      ORDER BY outbound_code LIMIT 3""")
    if not sample:
        print(json.dumps({"case": "T30_core_sample", "state": "not_verified", "reason": "no_sample"}))
        return 1
    codes = tuple(row["outbound_code"] for row in sample)
    queries = reference_queries(codes)
    before = expected_links(*(query(sql) for sql in queries))
    plan = OutboundLinkPlan(outbound_codes=codes)
    report = reconcile_outbound_links(query(compile_outbound_links(plan)), plan)
    after = expected_links(*(query(sql) for sql in queries))
    checks = {"independent_reference_stable": canonical(before) == canonical(after),
              "all_relationship_fields": canonical(report.rows) == canonical(before),
              "document_count": report.document_count == len({(r["cargo_owner"], r["outbound_code"]) for r in before}),
              "unmatched_document_count": report.unmatched_document_count == len({(r["cargo_owner"], r["outbound_code"]) for r in before if r["relationship_state"] == "unmatched"})}
    # Latest is a separately selected user mode, never a hidden sampling limit.
    # Work at the same bounded exact-code scope for this compiler check.
    dated = [row for row in before if row["ship_at"] is not None]
    latest_stamp = max((row["ship_at"] for row in dated), default=None)
    latest_key = min(((row["cargo_owner"], row["outbound_code"]) for row in dated
                      if row["ship_at"] == latest_stamp), default=None)
    expected_latest = [row for row in before if (row["cargo_owner"], row["outbound_code"]) == latest_key]
    latest_report = reconcile_outbound_links(query(compile_outbound_links(OutboundLinkPlan(outbound_codes=codes, latest=True))),
                                             OutboundLinkPlan(outbound_codes=codes, latest=True))
    latest_after = expected_links(*(query(sql) for sql in queries))
    checks["latest_all_sources_and_tie_order"] = canonical(latest_report.rows) == canonical(expected_latest)
    checks["latest_reference_stable"] = canonical(before) == canonical(latest_after)
    print(json.dumps({"case": "T30_core_sample", "scope": "first_3_yesterday_outbound_codes_all_owners", "checks": checks}))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case": "T30_core_sample", "state": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)
