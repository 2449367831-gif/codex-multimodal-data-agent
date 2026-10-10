"""SO07 SQL compatibility on fixed constants, never physical business sources."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.sales_order_details import SalesOrderDetailPlan, compile_sales_order_details, reconcile_sales_order_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.sql import SqlSafetyValidator


PLAN = SalesOrderDetailPlan(date(2026, 9, 10), date(2026, 9, 11))
HEADER = 'scm_sales_order_header'
DETAIL = 'scm_sales_order_detail'
FIELDS = {
    HEADER: ('id', 'company_code', 'sales_order_code', 'facility_code', 'store_code',
             'store_name', 'order_type', 'created', 'status', 'deleted_flag', '_fdl_marked_deleted', 'updated'),
    DETAIL: ('id', 'company_code', 'sales_order_code', 'facility_code', 'sku_code',
             'sku_name', 'request_qty', 'status', '_fdl_marked_deleted', 'sales_order_id',
             'updated', 'quantity_u_m', 'sales_unit'),
    'master_wms': ('cargo_owner_id', 'wms_code', 'wms_desc', 'status'),
}


def fixtures():
    return {
        HEADER: [dict(zip(FIELDS[HEADER], ('H1', 'A', 'S1', 'W1', 'C1', 'Customer', 'SO07',
                                          '2026-09-10 01:00:00', '10', 0, 'false', None)))],
        DETAIL: [dict(zip(FIELDS[DETAIL], ('D1', 'A', 'S1', 'W1', '001', 'Product',
                                          '123456789012345678901234567890.123456', '0', 'false', None, None, None, None))),
                 dict(zip(FIELDS[DETAIL], ('D2', 'A', 'S1', 'W1', '002', 'Product 2',
                                          '0.000001', '0', 'false', None, None, None, None)))],
        'master_wms': [dict(zip(FIELDS['master_wms'], ('A', 'W1', 'Warehouse', '0')))],
    }


def synthetic_sql(plan, rows, *, source_sql=None):
    tree = sqlglot.parse_one(source_sql if source_sql is not None else compile_sales_order_details(plan), read='doris')
    used = set()
    for table in tree.find_all(exp.Table):
        if table.db or table.catalog:
            if table.catalog or table.db != 'ywzt_ods' or table.name not in FIELDS:
                raise ValueError('unexpected physical source')
            used.add(table.name)
            table.set('this', exp.to_identifier('fixture_' + table.name))
            table.set('db', None)
    ctes = []
    for name in sorted(used):
        selects = []
        for row in rows[name]:
            if set(row) != set(FIELDS[name]):
                raise ValueError('invalid synthetic fields')
            expressions = []
            for field in FIELDS[name]:
                kind = ('DATETIME' if field in ('created', 'updated') else 'DECIMAL(38,6)' if field == 'request_qty'
                        else 'INT' if field == 'deleted_flag' else 'STRING')
                value = exp.Null() if row[field] is None else exp.Literal.string(str(row[field]))
                expressions.append(exp.alias_(exp.cast(value, kind), field))
            selects.append(exp.select(*expressions))
        if not selects:
            raise ValueError('at least one constant row required')
        relation = selects[0]
        for select in selects[1:]:
            relation = relation.union(select, distinct=False)
        ctes.append(exp.CTE(this=relation, alias=exp.TableAlias(this=exp.to_identifier('fixture_' + name))))
    existing = tree.args.get('with_')
    tree.set('with_', exp.With(expressions=ctes + (existing.expressions if existing else [])))
    sql = tree.sql(dialect='doris')
    SqlSafetyValidator().validate(sql, allowed_sources=frozenset())
    return sql


def cases():
    yield 'exact_decimal', PLAN, fixtures(), None
    for table, field, value in (
        (HEADER, 'company_code', None), (HEADER, 'sales_order_code', None),
        (HEADER, 'created', None), (HEADER, 'deleted_flag', None),
        (HEADER, 'deleted_flag', 7), (HEADER, '_fdl_marked_deleted', None),
        (HEADER, '_fdl_marked_deleted', 'invalid'), (DETAIL, '_fdl_marked_deleted', None),
        (DETAIL, '_fdl_marked_deleted', 'invalid'),
    ):
        rows = fixtures()
        rows[table][0][field] = value
        yield table + '_' + field + '_' + str(value), PLAN, rows, 'sales_order_quality_failed'
    for table, field, value in ((HEADER, 'deleted_flag', 1), (DETAIL, 'status', '1'),
                               (DETAIL, '_fdl_marked_deleted', 'true')):
        rows = fixtures()
        duplicate = deepcopy(rows[table][0])
        duplicate[field] = value
        rows[table].append(duplicate)
        yield table + '_excluded_' + field, PLAN, rows, None
    yield 'overflow_probe', replace(PLAN, page_limit=1), fixtures(), 'result_truncated'


def check_rows(rows, plan, expected_error):
    try:
        checked = reconcile_sales_order_details(rows, plan)
    except SemanticRuntimeError as exc:
        return exc.reason == expected_error
    return (expected_error is None and checked.order_count == 1 and checked.row_count == 2
            and checked.total_request_quantity == Decimal('123456789012345678901234567890.123457'))


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    checks = {}
    with bridge.request_session() as client:
        for name, plan, fixtures_rows, expected_error in cases():
            try:
                result = client.query(sql=synthetic_sql(plan, fixtures_rows), purpose='synthetic SO07 SQL compatibility')
                rows = validate_doris_result(result).rows
                checks[name] = check_rows(rows, plan, expected_error)
            except Exception as exc:
                reason = getattr(exc, 'reason', None)
                allowed = {'backend_unavailable', 'backend_rejected', 'query_failed', 'query_timeout', 'result_truncated'}
                print(json.dumps({'case': 'so07_synthetic_doris', 'checks': checks, 'failed_case': name,
                    'error_type': type(exc).__name__, 'reason': reason if reason in allowed else 'execution_invalid',
                    'business_source_reads': False}), flush=True)
                return 1
    print(json.dumps({'case': 'so07_synthetic_doris', 'checks': checks, 'business_source_reads': False}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'so07_synthetic_doris', 'passed': False,
                          'error_type': type(exc).__name__, 'business_source_reads': False}), flush=True)
        raise SystemExit(1)
