"""Independent raw-source SO07 reconstruction; stdout contains metadata only."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from fractions import Fraction
import json
import math
from pathlib import Path
import sys
import time as clock
from zoneinfo import ZoneInfo

from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.sales_order_details import SalesOrderDetailPlan, retrieve_sales_order_details
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.sql import SqlSafetyValidator


ZONE = ZoneInfo('Asia/Shanghai')
SOURCE_FIELDS = {
    'header': ('id', 'company_code', 'sales_order_code', 'facility_code', 'store_code',
               'store_name', 'order_type', 'created', 'updated', 'status', 'deleted_flag', '_fdl_marked_deleted'),
    'detail': ('id', 'sales_order_id', 'company_code', 'sales_order_code', 'facility_code',
               'sku_code', 'sku_name', 'request_qty', 'quantity_u_m', 'sales_unit', 'updated',
               'status', '_fdl_marked_deleted'),
    'warehouse': ('cargo_owner_id', 'wms_code', 'wms_desc', 'status'),
}
TABLES = {'header': 'scm_sales_order_header', 'detail': 'scm_sales_order_detail', 'warehouse': 'master_wms'}
OUTPUT_FIELDS = ('cargo_owner', 'sales_order_code', 'order_created_at', 'customer_code',
                 'customer_name', 'warehouse_code', 'warehouse_name', 'order_type', 'order_status',
                 'detail_id', 'sku_code', 'sku_name', 'request_quantity', 'header_updated_at',
                 'detail_updated_at', 'quantity_unit_reference', 'sales_unit_reference')


class ReferenceQualityError(ValueError):
    def __init__(self, reason):
        if reason not in {'relationship', 'header', 'warehouse', 'detail'}:
            raise ValueError('invalid reference failure category')
        super().__init__('Independent SO07 source validation failed')
        self.reason = reason


def reference_sql(plan, source):
    if not isinstance(plan, SalesOrderDetailPlan) or source not in TABLES:
        raise ValueError('invalid source reference plan')
    def literal(value):
        return exp.Literal.string(value).sql(dialect='doris')
    def key(alias, name):
        return f"NULLIF(TRIM(CAST({alias}.{name} AS STRING)),'')"
    window = 'TRUE' if plan.start is None else (
        f"(h.created IS NULL OR (h.created>={literal(str(plan.start)+' 00:00:00')} "
        f"AND h.created<{literal(str(plan.end)+' 00:00:00')}))")
    owner = 'TRUE' if plan.owner == 'ALL' else f"{key('h', 'company_code')}={literal(plan.owner)}"
    candidates = f"""SELECT h.id,h.company_code,h.sales_order_code,h.facility_code FROM ywzt_ods.scm_sales_order_header h
      WHERE TRIM(h.order_type)='SO07' AND (h.deleted_flag IS NULL OR h.deleted_flag<>1)
        AND (h._fdl_marked_deleted IS NULL OR LOWER(TRIM(h._fdl_marked_deleted))<>'true')
        AND {window} AND {owner}"""
    pair = f"{key('c','company_code')}<=>{key('r','company_code')} AND {key('c','sales_order_code')}<=>{key('r','sales_order_code')}"
    if source == 'header':
        condition = (f"(r.deleted_flag IS NULL OR r.deleted_flag<>1) AND "
            f"(r._fdl_marked_deleted IS NULL OR LOWER(TRIM(r._fdl_marked_deleted))<>'true') "
            f"AND EXISTS (SELECT 1 FROM candidates c WHERE {pair})")
    elif source == 'detail':
        paths = (pair, f"{key('c','id')}={key('r','sales_order_id')}",
                 f"{key('r','company_code')} IS NULL AND {key('r','sales_order_code')}={key('c','sales_order_code')}",
                 f"{key('r','sales_order_code')} IS NULL AND {key('r','company_code')}={key('c','company_code')}")
        reaches = ' OR '.join(f'EXISTS (SELECT 1 FROM candidates c WHERE {path})' for path in paths)
        condition = ("TRIM(CAST(r.status AS STRING))='0' AND "
            "(r._fdl_marked_deleted IS NULL OR LOWER(TRIM(r._fdl_marked_deleted))<>'true') "
            f"AND ({reaches})")
    else:
        condition = ("COALESCE(TRIM(CAST(r.status AS STRING)),'0')='0' AND EXISTS "
            f"(SELECT 1 FROM warehouse_keys k WHERE {key('k','company_code')}={key('r','cargo_owner_id')} "
            f"AND {key('k','facility_code')}={key('r','wms_code')})")
    fields = ','.join('r.' + name for name in SOURCE_FIELDS[source])
    with_clause = f'WITH candidates AS ({candidates}) '
    if source == 'warehouse':
        with_clause = f"""WITH candidates AS ({candidates}), warehouse_keys AS (
          SELECT company_code,facility_code FROM candidates
          UNION SELECT d.company_code,d.facility_code FROM ywzt_ods.scm_sales_order_detail d
          WHERE TRIM(CAST(d.status AS STRING))='0'
            AND (d._fdl_marked_deleted IS NULL OR LOWER(TRIM(d._fdl_marked_deleted))<>'true')
            AND EXISTS (SELECT 1 FROM candidates c
              WHERE {key('c','company_code')}={key('d','company_code')}
                AND {key('c','sales_order_code')}={key('d','sales_order_code')})) """
    sql = (f'{with_clause}SELECT {fields},COUNT(*) OVER() AS group_count '
           f'FROM ywzt_ods.{TABLES[source]} r WHERE {condition} ORDER BY {fields} LIMIT 501')
    SqlSafetyValidator().validate(sql, allowed_sources={'ywzt_ods.' + table for table in TABLES.values()})
    return sql


def text(value, *, blank=False):
    value = None if value is None else str(value).strip(' ')
    return None if blank and value == '' else value


def instant(value):
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZONE)
    return parsed.astimezone(ZONE).isoformat()


def quantity(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('invalid reference quantity')
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError('invalid reference quantity')
    return Fraction(number)


def canonical_rows(rows):
    result = []
    for row in rows:
        result.append(tuple(quantity(row[name]) if name == 'request_quantity' else
                            instant(row[name]) if name.endswith('_at') else row[name]
                            for name in OUTPUT_FIELDS))
    return tuple(sorted(result, key=lambda values: (values[0], values[1], values[9] or '')))


def expected_details(plan, sources):
    """Rebuild joins/filters in Python; no candidate SQL or result controls used."""
    headers, details, warehouses = defaultdict(list), [], defaultdict(set)
    for source in sources['header']:
        row = {name: text(source[name]) for name in SOURCE_FIELDS['header']}
        row['_fdl_marked_deleted'] = (row['_fdl_marked_deleted'].lower()
                                      if row['_fdl_marked_deleted'] is not None else None)
        if row['deleted_flag'] == '1' or (row['_fdl_marked_deleted'] or '').lower() == 'true':
            continue
        headers[(row['company_code'], row['sales_order_code'])].append(row)
    for source in sources['detail']:
        row = {name: text(source[name]) for name in SOURCE_FIELDS['detail']}
        if row['status'] == '0' and (row['_fdl_marked_deleted'] or '').lower() != 'true':
            details.append(row)
    for source in sources['warehouse']:
        if text(source['status']) not in (None, '0'):
            continue
        name = text(source['wms_desc'], blank=True)
        if name:
            warehouses[(text(source['cargo_owner_id']), text(source['wms_code']))].add(name)
    detail_counts = Counter((row['company_code'], row['sales_order_code'], row['id']) for row in details)
    start = datetime.combine(plan.start, time.min, ZONE) if plan.start else None
    end = datetime.combine(plan.end, time.min, ZONE) if plan.end else None
    result = []
    for (owner, order), versions in headers.items():
        if plan.owner != 'ALL' and owner != plan.owner:
            continue
        candidates = [row for row in versions if row['order_type'] == 'SO07' and
                      (start is None or row['created'] is None or
                       start <= datetime.fromisoformat(instant(row['created'])) < end)]
        if not candidates:
            continue
        header = candidates[0]
        physical_ids = {row['id'] or None for row in candidates}
        matching = []
        for row in details:
            detail_owner, detail_order, header_id = (text(row[name], blank=True)
                for name in ('company_code', 'sales_order_code', 'sales_order_id'))
            reaches = (header_id is not None and header_id in physical_ids) or (
                detail_owner == owner and detail_order == order) or (
                detail_owner is None and detail_order == order) or (
                detail_order is None and detail_owner == owner)
            if not reaches:
                continue
            warehouse = text(row['facility_code'], blank=True) or header['facility_code']
            sku = text(row['sku_code'], blank=True)
            if (plan.warehouse_codes and warehouse and warehouse not in plan.warehouse_codes) or (
                plan.sku_codes and sku and sku not in plan.sku_codes):
                continue
            if (not owner or not order or not detail_owner or not detail_order or
                (owner, order) != (detail_owner, detail_order) or
                (header_id is not None and physical_ids != {header_id})):
                raise ReferenceQualityError('relationship')
            matching.append(row)
        # A real detail outside a requested SKU/warehouse scope is not a missing
        # detail row. Apply scope after left-join semantics, not before the join.
        has_any_detail = any((row['company_code'], row['sales_order_code']) == (owner, order) for row in details)
        selected = matching if has_any_detail else [None]
        selected = [row for row in selected if
                    (not plan.warehouse_codes or ((text(row['facility_code'], blank=True) if row else None)
                     or header['facility_code']) in plan.warehouse_codes) and
                    (not plan.sku_codes or (row is not None and row['sku_code'] in plan.sku_codes))]
        if not selected:
            continue
        # Physical duplicate headers are permitted only when all projected
        # attributes agree. An excluded date/type version must still be checked.
        attributes = {tuple(text(row[name], blank=name == 'store_name') for name in SOURCE_FIELDS['header'] if name != 'id')
                      for row in versions}
        if (len(attributes) != 1 or not owner or not order or not header['created'] or
            header['deleted_flag'] != '0' or (header['_fdl_marked_deleted'] or '').lower() != 'false'):
            raise ReferenceQualityError('header')
        for row in selected:
            warehouse = (text(row['facility_code'], blank=True) if row else None) or header['facility_code']
            names = warehouses[(owner, warehouse)]
            if not warehouse or len(names) > 1:
                raise ReferenceQualityError('warehouse')
            if row is not None and (not row['id'] or not row['sku_code'] or
                detail_counts[(owner, order, row['id'])] != 1 or
                (row['_fdl_marked_deleted'] or '').lower() != 'false' or quantity(row['request_qty']) is None):
                raise ReferenceQualityError('detail')
            result.append(dict(zip(OUTPUT_FIELDS, (
                owner, order, instant(header['created']), header['store_code'], text(header['store_name'], blank=True),
                warehouse, next(iter(names), None), header['order_type'], header['status'],
                row['id'] if row else None, row['sku_code'] if row else None,
                text(row['sku_name'], blank=True) if row else None, row['request_qty'] if row else None,
                instant(header['updated']), instant(row['updated']) if row else None,
                text(row['quantity_u_m'], blank=True) if row else None, text(row['sales_unit'], blank=True) if row else None,
            ))))
    return canonical_rows(result)


def verify(plan, query, *, query_many=None, timeout_seconds=180, report=lambda stage: None):
    """Each of three independent reads has the unchanged query-workflow budget."""
    if (not isinstance(plan, SalesOrderDetailPlan) or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 180):
        raise ValueError('invalid verification budget or plan')
    deadline = clock.monotonic() + timeout_seconds
    durations = {}
    def remaining():
        value = deadline - clock.monotonic()
        if value <= 0:
            raise TimeoutError('SO07 verification deadline')
        return min(value, 180)
    def checked_query(sql, **kwargs):
        remaining()
        rows = query(sql, **kwargs)
        remaining()
        return rows
    def checked_many(statements, *, purpose, timeout_seconds):
        rows = query_many(statements, purpose=purpose, timeout_seconds=min(timeout_seconds, remaining()))
        remaining()
        return rows
    def reference(stage):
        source_rows = {}
        for source in SOURCE_FIELDS:
            report(stage + '.' + source)
            source_rows[source] = retrieve_sales_groups(reference_sql(plan, source), checked_query,
                query_many=checked_many if query_many else None,
                page_size=500, page_cap=500, timeout_seconds=remaining())
        report(stage + '.reconstruct')
        expected = expected_details(plan, source_rows)
        remaining()
        return expected
    def phase(name, operation):
        nonlocal deadline
        started = clock.monotonic()
        deadline = started + timeout_seconds
        report(name)
        result = operation()
        remaining()
        durations[name] = round(clock.monotonic() - started, 3)
        return result
    before = phase('reference_before', lambda: reference('reference_before'))
    result = phase('candidate', lambda: retrieve_sales_order_details(plan, checked_query,
        query_many=checked_many if query_many else None, timeout_seconds=remaining()))
    after = phase('reference_after', lambda: reference('reference_after'))
    checks = {'source_stable': before == after, 'exact_rows': canonical_rows(result.rows) == before,
              'order_count': result.order_count == len({(row[0], row[1]) for row in before}),
              'source_freshness_not_inferred': result.source_evidence.ingestion_freshness == 'unverified',
              'unit_conversion_not_inferred': result.source_evidence.quantity_unit_status == 'unconfirmed'}
    return {'case': 'so07_actual_source_reference', 'passed': all(checks.values()), 'checks': checks,
            'nonempty_verified': bool(before) and all(checks.values()), 'business_source_reads': True,
            'publication_changed': False, 'phase_seconds': durations,
            'phase_budget_seconds': timeout_seconds}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=True, type=date.fromisoformat)
    parser.add_argument('--end', required=True, type=date.fromisoformat)
    args = parser.parse_args()
    plan = SalesOrderDetailPlan(args.start, args.end)
    _hide_windows_subprocesses()
    stage = 'setup'
    started = clock.monotonic()
    def report(value):
        nonlocal stage
        stage = value
        print(json.dumps({'stage': stage, 'elapsed_seconds': round(clock.monotonic() - started, 2)}), flush=True)
    try:
        bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
        with bridge.request_session() as client:
            def query(sql, **kwargs):
                return validate_doris_result(client.query(sql=sql, purpose='independent SO07 actual-source acceptance')).rows
            def query_many(statements, *, purpose, timeout_seconds):
                payloads = client.query_many(statements=statements,
                    purpose='independent SO07 actual-source acceptance', timeout_seconds=timeout_seconds)
                return [validate_doris_result(payload).rows for payload in payloads]
            outcome = verify(plan, query, query_many=query_many, report=report)
        print(json.dumps(outcome), flush=True)
        return 0 if outcome['passed'] else 1
    except Exception as exc:
        allowed_reasons = {'backend_unavailable', 'backend_rejected', 'query_failed', 'query_timeout',
                           'result_truncated', 'sales_order_reference_failed', 'sales_order_quality_failed',
                           'sales_page_invalid'}
        reason = getattr(exc, 'reason', None)
        print(json.dumps({'case': 'so07_actual_source_reference', 'passed': False,
                          'stage': stage, 'error_type': type(exc).__name__,
                          'reason': reason if reason in allowed_reasons else None,
                          'reference_failure': exc.reason if isinstance(exc, ReferenceQualityError) else None,
                          'business_source_reads': True, 'publication_changed': False}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
