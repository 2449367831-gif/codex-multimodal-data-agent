"""Bounded SO07 paging over typed Doris constants; no business-source reads."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.sales_order_details import retrieve_sales_order_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

FIXTURE = runpy.run_path(str(ROOT / 'tests/acceptance/check_so07_sql_live.py'))
PLAN = replace(FIXTURE['PLAN'], page_limit=1)


def fixture_cases():
    yield 'sequential', PLAN, FIXTURE['fixtures'](), {}, (2, 1, '123456789012345678901234567890.123457')
    yield 'batched', PLAN, FIXTURE['fixtures'](), {'batched': True}, (2, 1, '123456789012345678901234567890.123457')
    rows = FIXTURE['fixtures']()
    second = {**rows[FIXTURE['HEADER']][0], 'id': 'H2', 'sales_order_code': 'S2'}
    other = {**rows[FIXTURE['HEADER']][0], 'id': 'HB', 'company_code': 'B'}
    rows[FIXTURE['HEADER']].extend([second, other])
    rows[FIXTURE['DETAIL']].append({**rows[FIXTURE['DETAIL']][0], 'company_code': 'B', 'request_qty': '5'})
    rows['master_wms'].append({**rows['master_wms'][0], 'cargo_owner_id': 'B'})
    yield 'multiple_owners_missing_detail', PLAN, rows, {}, (4, 3, '123456789012345678901234567895.123457')
    yield 'exact_scope', replace(PLAN, owner='A', sku_codes=('001',)), rows, {}, (1, 1, '123456789012345678901234567890.123456')
    yield 'empty', replace(PLAN, owner='MISSING'), rows, {}, (0, 0, '0')
    yield 'row_budget', PLAN, rows, {'max_rows': 1}, 'result_truncated'
    yield 'changed_between_passes', PLAN, FIXTURE['fixtures'](), {'change_after': 3}, 'sales_page_invalid'
    invalid = FIXTURE['fixtures']()
    invalid[FIXTURE['HEADER']][0]['_fdl_marked_deleted'] = None
    yield 'invalid_source', PLAN, invalid, {}, 'sales_order_quality_failed'
    all_time = FIXTURE['fixtures']()
    all_time[FIXTURE['HEADER']].append({**all_time[FIXTURE['HEADER']][0],
        'id': 'H-OLD', 'sales_order_code': 'OLD', 'created': '2020-01-01 00:00:00'})
    yield 'all_time_keeps_old_order_without_detail', replace(PLAN, start=None, end=None), all_time, {}, (3, 2, '123456789012345678901234567890.123457')
    yield 'dated_excludes_old_order', PLAN, all_time, {}, (2, 1, '123456789012345678901234567890.123457')
    yield 'all_time_exact_sku', replace(PLAN, start=None, end=None, sku_codes=('001',)), all_time, {}, (1, 1, '123456789012345678901234567890.123456')
    all_time[FIXTURE['DETAIL']].append({**all_time[FIXTURE['DETAIL']][0], 'id': 'D-OLD',
        'sales_order_id': 'H-OLD', 'sales_order_code': 'OLD', 'company_code': 'OTHER'})
    yield 'all_time_checks_old_reference_conflict', replace(PLAN, start=None, end=None), all_time, {}, 'sales_order_reference_failed'


def run_cases(client):
    checks = {}
    for name, plan, source, settings, expected in fixture_cases():
        rows, calls, quality_calls = deepcopy(source), [], []
        def prepare(sql, *, quality=False):
            (quality_calls if quality else calls).append(sql)
            if not quality and len(calls) == settings.get('change_after'):
                rows[FIXTURE['HEADER']][0]['store_name'] = 'Synthetic changed customer'
            return FIXTURE['synthetic_sql'](plan, rows, source_sql=sql)
        def query(sql, **kwargs):
            result = client.query(sql=prepare(sql, quality=kwargs.get('purpose') == 'SO07 reference integrity'),
                purpose='synthetic SO07 bounded page or reference control')
            return validate_doris_result(result).rows
        def query_many(statements, **kwargs):
            responses = client.query_many(statements=[prepare(sql) for sql in statements],
                purpose='synthetic SO07 bounded page batch', timeout_seconds=kwargs['timeout_seconds'])
            return [validate_doris_result(result).rows for result in responses]
        try:
            result = retrieve_sales_order_details(plan, query,
                query_many=query_many if settings.get('batched') else None,
                max_rows=settings.get('max_rows', 100000))
            checks[name] = (not isinstance(expected, str)
                and (result.row_count, result.order_count, result.total_request_quantity)
                == (expected[0], expected[1], Decimal(expected[2]))
                and len(calls) == 2 * max(1, result.row_count) and len(quality_calls) == 2)
            if name == 'multiple_owners_missing_detail':
                checks[name] = checks[name] and ({r['cargo_owner'] for r in result.rows} == {'A', 'B'})
                checks[name] = checks[name] and any(r['request_quantity'] is None for r in result.rows)
        except SemanticRuntimeError as exc:
            checks[name] = exc.reason == expected
            if name == 'row_budget':
                checks[name] = checks[name] and len(calls) == 1
    return checks


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    with bridge.request_session() as client:
        checks = run_cases(client)
    print(json.dumps({'case': 'so07_paging_synthetic_doris', 'checks': checks, 'business_source_reads': False}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'so07_paging_synthetic_doris', 'passed': False,
                          'error_type': type(exc).__name__, 'business_source_reads': False}), flush=True)
        raise SystemExit(1)
