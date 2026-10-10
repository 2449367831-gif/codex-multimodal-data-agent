"""Independent bounded employee reference check; never persist business rows."""

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.dms_manager_lookup import retrieve_manager_lookup
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


def reference_id(raw):
    if raw is None:
        return None
    text = raw.strip(' ')
    digits = text[1:] if text[:1] in ('+', '-') else text
    if not digits or any(char not in '0123456789' for char in digits):
        return None
    significant = digits.lstrip('0') or '0'
    if len(significant) > 19:
        return None
    number = int(significant) * (-1 if text.startswith('-') else 1)
    return str(number) if -9223372036854775808 <= number <= 9223372036854775807 else None


def reference_sql(refs):
    keys = sorted({reference_id(raw) for raw in refs} - {None})
    predicate = 'CAST(employee_id AS STRING) IN (' + ','.join("'" + key + "'" for key in keys) + ')' if keys else 'FALSE'
    return f"""WITH observations AS (
 SELECT employee_id,actual_name,deleted_flag,update_time,COUNT(*) AS observations
 FROM dms_ods.t_employee WHERE {predicate}
 GROUP BY employee_id,actual_name,deleted_flag,update_time
)
SELECT *,COUNT(*) OVER () AS group_count FROM observations
ORDER BY employee_id,update_time,actual_name,deleted_flag LIMIT 501"""


def timestamp(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        from zoneinfo import ZoneInfo
        parsed = parsed.astimezone(ZoneInfo('Asia/Shanghai'))
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


def expected_lookup(observations, refs):
    employees = defaultdict(list)
    for row in observations:
        employees[str(row['employee_id'])].append(row)
    result = []
    for raw in refs:
        key = reference_id(raw)
        group = employees.get(key, [])
        name, stamp, variants, sources = None, None, 0, 0
        if raw is None or not raw.strip(' '):
            state = 'missing_reference'
        elif key is None:
            state = 'invalid_reference'
        elif not group:
            state = 'not_found'
        else:
            stamps = [timestamp(row['update_time']) for row in group if row['update_time'] is not None]
            if not stamps:
                raise ValueError('missing reference version timestamp')
            stamp = max(stamps)
            latest = [row for row in group if row['update_time'] is not None and timestamp(row['update_time']) == stamp]
            tuples = {(row['actual_name'].strip(' ') or None if row['actual_name'] is not None else None, row['deleted_flag']) for row in latest}
            variants, sources = len(tuples), sum(int(row['observations']) for row in latest)
            if variants > 1:
                state = 'conflicting_employee'
            else:
                current_name, deleted = next(iter(tuples))
                if deleted not in (0, 1):
                    state = 'invalid_employee_state'
                elif deleted == 1:
                    state = 'deleted_employee'
                elif current_name is None:
                    state = 'missing_name'
                else:
                    state, name = 'matched', current_name
        result.append({'manager_reference': raw, 'employee_id': key, 'manager_name': name,
                       'match_state': state, 'variant_count': variants, 'source_rows': sources,
                       'source_updated': stamp})
    return result


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    def query(sql, *, purpose):
        return validate_doris_result(bridge.query(sql=sql, purpose=purpose)).rows
    references = query("SELECT DISTINCT owner_manager FROM dms_ods.t_sales_order WHERE owner_manager IS NOT NULL ORDER BY owner_manager LIMIT 20",
                       purpose='T26 bounded integration references; not complete order statistics')
    refs = tuple(dict.fromkeys([row['owner_manager'] for row in references] + [None, '', 'invalid-manager-reference']))
    def reference():
        return expected_lookup(retrieve_sales_groups(reference_sql(refs), query), refs)
    before = reference()
    actual = retrieve_manager_lookup(refs, query)
    after = reference()
    checks = {'bounded_order_references_present': bool(references), 'reference_stable': before == after,
              'all_reference_fields_match': actual == before, 'no_dropped_or_multiplied_references': len(actual) == len(refs)}
    print(json.dumps({'case': 'T26_lookup_only', 'checks': checks}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'T26_lookup_only', 'passed': False, 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1)
