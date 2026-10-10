"""SO07 source-evidence contracts checked against typed Doris constants."""

from dataclasses import replace
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.sales_order_details import retrieve_sales_order_details
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

FIXTURE = runpy.run_path(str(ROOT / 'tests/acceptance/check_so07_sql_live.py'))
PLAN, HEADER, DETAIL = replace(FIXTURE['PLAN'], page_limit=1), FIXTURE['HEADER'], FIXTURE['DETAIL']
EVIDENCE_FIELDS = ('missing_header_updated_count', 'missing_detail_updated_count',
                  'missing_quantity_unit_reference_count', 'missing_sales_unit_reference_count',
                  'source_header_latest_updated', 'source_detail_latest_updated', 'unit_reference_pairs')


def fixtures():
    rows = FIXTURE['fixtures']()
    # Source-evidence cases use integer quantities; the separate Doris decimal
    # stress fixtures remain unchanged and are not representable in SQLite.
    for index, row in enumerate(rows[DETAIL], start=1):
        row['request_qty'] = str(index)
    return rows


def cases():
    yield 'all_missing', PLAN, fixtures(), (1, 2, 2, 2, None, None, ((None, None, 2),))
    rows = fixtures()
    rows[HEADER][0]['updated'] = '2026-09-11 01:00:00'
    rows[DETAIL][0].update(updated='2026-09-11 02:00:00', quantity_u_m=' 010 ', sales_unit=' 040 ')
    rows[DETAIL][1].update(updated='2026-09-11 03:00:00', quantity_u_m='020', sales_unit='040')
    yield 'mixed_units_later_updates', PLAN, rows, (0, 0, 0, 0, '2026-09-11T01:00:00+08:00',
        '2026-09-11T03:00:00+08:00', (('010', '040', 1), ('020', '040', 1)))
    yield 'exact_sku', replace(PLAN, sku_codes=('001',)), rows, (0, 0, 0, 0,
        '2026-09-11T01:00:00+08:00', '2026-09-11T02:00:00+08:00', (('010', '040', 1),))
    yield 'no_data', replace(PLAN, owner='MISSING'), rows, (0, 0, 0, 0, None, None, ())
    rows = fixtures()
    for row in rows[DETAIL]:
        row.update(status='1', quantity_u_m='IGNORED', updated='2026-09-11 03:00:00')
    yield 'no_active_detail', PLAN, rows, (1, 0, 0, 0, None, None, ())
    rows = fixtures()
    rows[DETAIL][0].update(quantity_u_m=' ', sales_unit='040')
    rows[DETAIL][1].update(quantity_u_m='010', sales_unit=' ')
    yield 'blank_unit_references', PLAN, rows, (1, 2, 1, 1, None, None, ((None, '040', 1), ('010', None, 1)))
    rows = fixtures()
    rows[HEADER].append({**rows[HEADER][0], 'id': 'HB', 'company_code': 'B', 'updated': '2026-09-11 01:00:00'})
    rows[DETAIL].append({**rows[DETAIL][0], 'company_code': 'B', 'updated': '2026-09-11 02:00:00'})
    rows['master_wms'].append({**rows['master_wms'][0], 'cargo_owner_id': 'B'})
    yield 'same_order_code_other_owner', PLAN, rows, (1, 2, 3, 3,
        '2026-09-11T01:00:00+08:00', '2026-09-11T02:00:00+08:00', ((None, None, 3),))


def check_case(plan, source, expected, query):
    def read(sql, **kwargs):
        return query(FIXTURE['synthetic_sql'](plan, source, source_sql=sql))
    result = retrieve_sales_order_details(plan, read)
    evidence = result.source_evidence
    return (tuple(getattr(evidence, name) for name in EVIDENCE_FIELDS) == expected
        and evidence.ingestion_freshness == 'unverified' and evidence.quantity_unit_status == 'unconfirmed')


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    with bridge.request_session() as client:
        def query(sql):
            return validate_doris_result(client.query(sql=sql, purpose='synthetic SO07 source evidence')).rows
        checks = {name: check_case(plan, source, expected, query) for name, plan, source, expected in cases()}
    print(json.dumps({'case': 'so07_source_evidence_synthetic_doris', 'checks': checks,
                      'business_source_reads': False}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'so07_source_evidence_synthetic_doris', 'passed': False,
                          'error_type': type(exc).__name__, 'business_source_reads': False}), flush=True)
        raise SystemExit(1)
