"""SO07 reference diagnostics on typed constants, never business rows."""

from dataclasses import replace
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.sales_order_references import compile_sales_order_reference_quality, REFERENCE_FLAGS
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

FIXTURE = runpy.run_path(str(ROOT / 'tests/acceptance/check_so07_sql_live.py'))
PLAN, HEADER, DETAIL = FIXTURE['PLAN'], FIXTURE['HEADER'], FIXTURE['DETAIL']


def cases():
    def sql(rows, plan=PLAN):
        return FIXTURE['synthetic_sql'](plan, rows,
            source_sql=compile_sales_order_reference_quality(plan))
    yield 'valid_optional_reference', sql(FIXTURE['fixtures']()), False
    for field, value in (('company_code', None), ('company_code', ' '),
                         ('sales_order_code', None), ('sales_order_code', ' '),
                         ('company_code', 'OTHER'), ('sales_order_code', 'OTHER'),
                         ('sales_order_id', 'MISSING')):
        rows = FIXTURE['fixtures']()
        rows[DETAIL][0]['sales_order_id'] = 'H1'
        rows[DETAIL][0][field] = value
        yield field + '_' + str(value), sql(rows), True
    for field in ('company_code', 'sales_order_code'):
        rows = FIXTURE['fixtures']()
        rows[DETAIL][0][field] = None
        yield 'partial_without_id_' + field, sql(rows, replace(PLAN, sku_codes=('001',))), True
    for owner in ('A', 'B'):
        rows = FIXTURE['fixtures']()
        rows[HEADER].append({**rows[HEADER][0], 'id': 'HB', 'company_code': 'B', 'sales_order_code': 'S2'})
        rows[DETAIL][0]['sales_order_id'] = 'HB'
        yield 'reverse_conflict_' + owner, sql(rows, replace(PLAN, owner=owner)), True
    for scope in (dict(owner='B'), dict(warehouse_codes=('W2',)), dict(sku_codes=('002',))):
        rows = FIXTURE['fixtures']()
        rows[DETAIL][0]['sales_order_id'] = 'MISSING'
        yield 'unrelated_' + next(iter(scope)), sql(rows, replace(PLAN, **scope)), False
    for field, value in (('status', '1'), ('_fdl_marked_deleted', 'true')):
        rows = FIXTURE['fixtures']()
        rows[DETAIL][0].update(sales_order_id='MISSING', **{field: value})
        yield 'excluded_' + field, sql(rows), False
    for field, value in (('order_type', 'JYCK'), ('created', '2026-09-09 23:59:59'), ('deleted_flag', 1)):
        rows = FIXTURE['fixtures']()
        rows[HEADER][0][field] = value
        rows[DETAIL][0]['sales_order_id'] = 'MISSING'
        yield 'outside_header_' + field, sql(rows), False
    rows = FIXTURE['fixtures']()
    rows[DETAIL][0].update(sales_order_id=' H1 ', company_code=' A ', sales_order_code=' S1 ')
    yield 'outer_space_normalization', sql(rows), False


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    checks = {}
    with bridge.request_session() as client:
        for name, sql, expected in cases():
            result = client.query(sql=sql, purpose='synthetic SO07 reference integrity')
            rows = validate_doris_result(result).rows
            checks[name] = (len(rows) == 1 and set(rows[0]) == set(REFERENCE_FLAGS)
                and all(str(value) in ('0', '1') for value in rows[0].values())
                and any(int(value) for value in rows[0].values()) == expected)
    print(json.dumps({'case': 'so07_reference_synthetic_doris', 'checks': checks,
                      'business_source_reads': False}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'so07_reference_synthetic_doris', 'passed': False,
                          'error_type': type(exc).__name__, 'business_source_reads': False}), flush=True)
        raise SystemExit(1)
