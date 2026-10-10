"""Strict return-reference checks using only synthetic constant CTEs in Doris."""

import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.return_components import ReturnComponentPlan, compile_return_quality
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

SYNTHETIC = runpy.run_path(str(ROOT / 'tests/acceptance/check_reference_scope_sql_live.py'))['synthetic_sql']


def cases():
    stamp = '2026-09-01 00:00:00'
    plans = {
        'all': ReturnComponentPlan(),
        'order': ReturnComponentPlan(order_codes=('S1',)),
        'pair': ReturnComponentPlan(order_keys=(('A', 'S1'),)),
        'empty': ReturnComponentPlan(order_keys=()),
    }
    for variant, header_order, detail_order in (
        ('equal', 'S1', 'S1'), ('forward', 'S1', 'OTHER'),
        ('reverse', 'OTHER', 'S1'), ('missing_header', None, 'S1'),
        ('missing_detail', 'S1', None), ('blank_detail', 'S1', '   '),
    ):
        fixtures = {
            'scm_return_order_header': [('A', 'R1', header_order, 100, stamp, 'false')],
            'scm_return_order_detail': [('D1', 'A', 'R1', detail_order, '001', 3, 0, stamp, 'false')],
        }
        for scope, plan in plans.items():
            yield (variant + '_' + scope, SYNTHETIC(compile_return_quality(plan), fixture_rows=fixtures),
                   variant != 'equal' and scope != 'empty')


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    checks = {}
    with bridge.request_session() as client:
        for name, sql, must_reject in cases():
            rows = validate_doris_result(client.query(sql=sql, purpose='synthetic strict return reference verification')).rows
            checks[name] = (len(rows) == 1 and
                            bool(int(rows[0]['reference_conflict_count'])) == must_reject)
    print(json.dumps({'case': 'return_agreement_synthetic_doris', 'checks': checks,
                      'business_source_reads': False, 'published_business_acceptance': False}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'return_agreement_synthetic_doris', 'passed': False,
                          'error_type': type(exc).__name__, 'business_source_reads': False}), flush=True)
        raise SystemExit(1)
