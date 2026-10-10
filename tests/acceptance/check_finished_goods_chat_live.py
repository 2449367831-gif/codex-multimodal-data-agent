"""Running-chat acceptance for T07. Only boolean evidence leaves memory."""

from datetime import datetime, timedelta
import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from check_finished_goods_live import compare, independent_sql


def main():
    _hide_windows_subprocesses()
    base = 'http://127.0.0.1:8021'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type':'application/json','Origin':base,**(headers or {})})
        with opener.open(req, timeout=30) as response:
            return json.load(response)
    session = request('/v1/session')
    headers = {'X-CSRF-Token':session['csrf_token'],'Idempotency-Key':'finished-acceptance-' + uuid4().hex}
    cases = json.loads((ROOT / 'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))['cases']
    deferred_body = {'question':next(case['question'] for case in cases if case['id'] == 'T04'), 'conversation_id':session['conversation_id']}
    deferred_headers = {**headers, 'Idempotency-Key':'deferred-comparison-' + uuid4().hex}
    deferred = request('/v1/tasks', deferred_body, deferred_headers)
    deferred_deadline = time.monotonic() + 30
    while deferred['state'] in {'queued','running'} and time.monotonic() < deferred_deadline:
        time.sleep(0.2)
        deferred = request('/v1/tasks/' + deferred['task_id'])
    deferred_result = deferred.get('result', {})
    deferred_error = deferred_result.get('error', {})
    deferred_repeat = request('/v1/tasks', deferred_body, deferred_headers)
    deferred_checks = {
        'unpublished':deferred.get('http_status') == 409 and deferred_error.get('code') == 'business_definition_unpublished',
        'not_a_data_result':not deferred_result.get('business_table') and deferred_error.get('result_state') == 'blocked',
        'idempotent':deferred_repeat['task_id'] == deferred['task_id'],
    }
    print(json.dumps({'check':'T04_deferred_not_numeric_acceptance','checks':deferred_checks}), flush=True)
    if not all(deferred_checks.values()):
        return 1
    question = next(case['question'] for case in cases if case['id'] == 'T07')
    body = {'question':question,'conversation_id':session['conversation_id']}
    task = request('/v1/tasks',body,headers)
    print(json.dumps({'task_id':task['task_id'],'state':task['state']}),flush=True)
    deadline = time.monotonic() + 300
    while task['state'] in {'queued','running'}:
        if time.monotonic() >= deadline:
            print(json.dumps({'state':'observation_timeout','task_id':task['task_id']}))
            return 2
        time.sleep(1)
        task = request('/v1/tasks/' + task['task_id'])
    result = task.get('result',{})
    if task.get('http_status') != 200 or result.get('result_state') not in {'live_success','no_data'}:
        print(json.dumps({'state':task['state'],'http_status':task.get('http_status'),'result_state':result.get('result_state'),'error_code':result.get('error',{}).get('code')}))
        return 1
    table = result['business_table']
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    bridge = CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    reference = validate_doris_result(bridge.query(sql=independent_sql(today-timedelta(days=1),today),purpose='T07 running-chat independent reconciliation')).rows
    checks = compare(table,reference)
    repeat = request('/v1/tasks',body,headers)
    checks.update(no_clarification=result['clarification']['required'] is False,
                  same_conversation=result['conversation_id']==session['conversation_id'],
                  idempotent_result=repeat['task_id']==task['task_id'],
                  public_fields={col['key'] for col in table['columns']}=={'sku_code','quantity','outbound_count'})
    print(json.dumps({'case':'T07','checks':checks}))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case':'T07','state':'verification_failed','error_type':type(exc).__name__}))
        raise SystemExit(2)
