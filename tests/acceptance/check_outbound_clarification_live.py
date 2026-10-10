"""T32 selected actual outbound: live chat and an independent document aggregate.

Only boolean evidence is printed. Never persist Doris result rows.
"""

from datetime import datetime, timedelta
from decimal import Decimal
import argparse
import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


def reference_sql(start, end, *, owner=None, warehouse_code=None, group_by_warehouse=False):
    scope = []
    for field, value in [('company_code', owner), ('facility_code', warehouse_code)]:
        if value is not None:
            scope.append(f"TRIM(h.{field}) = {exp.Literal.string(value).sql(dialect='doris')}")
    predicate = ''.join(f' AND {item}' for item in scope)
    warehouse_select = ', TRIM(h.facility_code) AS warehouse_code' if group_by_warehouse else ''
    document_warehouse = ', e.warehouse_code' if group_by_warehouse else ''
    result = ("SELECT owner_code AS cargo_owner,warehouse_code,SUM(quantity) AS quantity,"
              "COUNT(*) AS outbound_count FROM documents GROUP BY owner_code,warehouse_code "
              "ORDER BY owner_code,warehouse_code LIMIT 501" if group_by_warehouse else
              "SELECT SUM(quantity) AS quantity,COUNT(*) AS outbound_count FROM documents LIMIT 1")
    return f"""WITH eligible AS (
  SELECT DISTINCT TRIM(h.company_code) AS owner_code, TRIM(h.outbound_code) AS document_code{warehouse_select}
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE h.ship_at >= '{start.isoformat()} 00:00:00' AND h.ship_at < '{end.isoformat()} 00:00:00'
    AND h.status IN (700,900,701,901)
    AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%')
    AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
    {predicate}
    AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header AS so
      WHERE TRIM(so.company_code)=TRIM(h.company_code) AND TRIM(so.sales_order_code)=TRIM(h.ref_order_code)
        AND TRIM(so.sales_order_code)<>'' AND COALESCE(so.deleted_flag,0)=0
        AND IFNULL(so._fdl_marked_deleted,'false')='false')
), documents AS (
  SELECT e.owner_code,e.document_code{document_warehouse},SUM(d.ship_qty) AS quantity
  FROM eligible AS e JOIN ywzt_ods.scm_sales_shipment_detail AS d
    ON TRIM(d.company_code)=e.owner_code AND TRIM(d.outbound_code)=e.document_code
  WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
  GROUP BY e.owner_code,e.document_code{document_warehouse}
)
{result}"""


def compare(table, rows, *, group_by_warehouse=False):
    if group_by_warehouse:
        def keyed(values):
            return {(row['cargo_owner'], row['warehouse_code']):
                    (Decimal(str(row['quantity'])), int(row['outbound_count'])) for row in values}
        actual, reference = keyed(table['rows']), keyed(rows)
        return len(actual) == len(table['rows']) and len(reference) == len(rows) and actual == reference
    control = rows[0]
    actual = table['rows']
    if int(control['outbound_count']) == 0:
        return not actual
    return (len(actual) == 1 and Decimal(actual[0]['quantity']) == Decimal(str(control['quantity']))
            and int(actual[0]['outbound_count']) == int(control['outbound_count']))


def matches_original_window(table, original_day):
    window = f"{(original_day - timedelta(days=1)).isoformat()} 00:00 至 {original_day.isoformat()} 00:00（不含结束时点）"
    caption = table.get('caption')
    return isinstance(caption, str) and caption.startswith(window)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scoped', action='store_true', help='Verify HJXH Changsha Huangxing warehouse continuation')
    parser.add_argument('--measure', choices=['outbound_actual_qty', 'outbound_document_count'], default='outbound_actual_qty')
    args = parser.parse_args()
    _hide_windows_subprocesses()
    base = 'http://127.0.0.1:8021'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'Origin': base, **(headers or {})})
        with opener.open(req, timeout=30) as response:
            return json.load(response)
    def wait(task):
        deadline = time.monotonic() + 300
        while task['state'] in {'queued', 'running'}:
            if time.monotonic() > deadline:
                print(json.dumps({'observation_timeout': True, 'task_id': task['task_id']}), flush=True)
                raise TimeoutError()
            time.sleep(1)
            task = request('/v1/tasks/' + task['task_id'])
        return task
    session = request('/v1/session')
    if session.get('clarification_pending'):
        raise ValueError('existing user clarification must remain untouched')
    cases = json.loads((ROOT / 'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))['cases']
    question = next(case['question'] for case in cases if case['id'] == 'T32')
    if args.scoped:
        question = '昨天HJXH长沙黄兴出库量，按仓库汇总'
    headers = {'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'outbound-first-' + uuid4().hex}
    first = wait(request('/v1/tasks', {'question': question, 'conversation_id': session['conversation_id']}, headers))
    original_day = datetime.fromtimestamp(first['created_at'], ZoneInfo('Asia/Shanghai')).date()
    assert first['http_status'] == 200
    session['conversation_id'] = first['result']['conversation_id']
    card = first['result']['clarification']
    assert card['required'] and [len(q['options']) for q in card['questions']] == [5, 5, 3]
    assert 'business_table' not in first['result']
    resumed = request('/v1/clarification/resume', {'conversation_id': session['conversation_id']}, headers)['clarification']
    assert resumed['questions'] == card['questions']
    selected = {'conversation_id': session['conversation_id'], 'clarification_token': resumed['token'],
        'clarification_answers': [{'question_id': key, 'option_id': value} for key, value in zip(
            ('outbound_business', 'outbound_measure', 'outbound_time_axis'),
            ('outbound_sales', args.measure, 'outbound_shipped_at'))]}
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    sql = reference_sql(original_day - timedelta(days=1), original_day,
                        owner='HJXH' if args.scoped else None,
                        warehouse_code='10114790' if args.scoped else None,
                        group_by_warehouse=args.scoped)
    before = validate_doris_result(bridge.query(sql=sql, purpose='independent T32 document aggregate before')).rows
    selected_headers = {**headers, 'Idempotency-Key': 'outbound-selected-' + uuid4().hex}
    task = wait(request('/v1/tasks', selected, selected_headers))
    assert task['http_status'] == 200
    result = task['result']
    assert result['result_state'] in {'live_success', 'no_data'}
    after = validate_doris_result(bridge.query(sql=sql, purpose='independent T32 document aggregate after')).rows
    checks = {'three_decisions': True, 'resumed_card': True,
        'stable_reference': before == after,
        'quantity_and_documents_reconciled': compare(result['business_table'], before, group_by_warehouse=args.scoped),
        'nonempty_evidence': bool(result['business_table']['rows']),
        'original_request_natural_day': matches_original_window(result['business_table'], original_day),
        'no_repeat_questions': not result['clarification']['required'],
        'same_conversation': result['conversation_id'] == session['conversation_id'],
        'idempotent': request('/v1/tasks', selected, selected_headers)['task_id'] == task['task_id']}
    print(json.dumps({'case': 'T32', 'scoped': args.scoped, 'measure': args.measure, 'checks': checks}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'reason': getattr(exc, 'reason', None)}), flush=True)
        raise SystemExit(2)
