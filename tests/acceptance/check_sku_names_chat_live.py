"""Exact T09 prompt, one selection, screened result and independent reference."""

import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.request
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from check_sku_names_live import reference_sql,compare


def main():
    _hide_windows_subprocesses()
    base='http://127.0.0.1:8021'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path,body=None,headers=None):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','Origin':base,**(headers or {})})
        with opener.open(req,timeout=30) as response:
            return json.load(response)
    def wait(task):
        print(json.dumps({'task_id':task['task_id'],'state':task['state']}),flush=True)
        deadline=time.monotonic()+300
        while task['state'] in {'queued','running'}:
            if time.monotonic()>=deadline:
                print(json.dumps({'task_id':task['task_id'],'state':'observation_timeout'}),flush=True)
                raise TimeoutError('observation expired')
            time.sleep(1)
            task=request('/v1/tasks/'+task['task_id'])
        return task
    session=request('/v1/session')
    headers={'X-CSRF-Token':session['csrf_token'],'Idempotency-Key':'names-first-'+uuid4().hex}
    cases=json.loads((ROOT/'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))['cases']
    question=next(c['question'] for c in cases if c['id']=='T09')
    first=wait(request('/v1/tasks',{'question':question,'conversation_id':session['conversation_id']},headers))
    first_result=first.get('result',{})
    card=first_result.get('clarification',{})
    if first.get('http_status')!=200 or not card.get('required') or len(card.get('questions',[]))!=1 or 'business_table' in first_result:
        print(json.dumps({'stage':'clarification_failed','state':first_result.get('result_state'),'http_status':first.get('http_status')}))
        return 1
    if card['questions'][0]['id']!='sku_name_outbound_basis':
        raise ValueError('unrelated choice')
    selected={'conversation_id':session['conversation_id'],'clarification_token':card['token'],
        'clarification_answers':[{'question_id':'sku_name_outbound_basis','option_ids':['sku_name_actual']}]}
    selected_headers={**headers,'Idempotency-Key':'names-selected-'+uuid4().hex}
    task=wait(request('/v1/tasks',selected,selected_headers))
    result=task.get('result',{})
    if task.get('http_status')!=200 or result.get('result_state') not in {'live_success','no_data'}:
        print(json.dumps({'stage':'selected_failed','state':result.get('result_state'),'http_status':task.get('http_status'),'code':result.get('error',{}).get('code')}))
        return 1
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    reference=validate_doris_result(bridge.query(sql=reference_sql(),purpose='T09 actual chat independent reference')).rows
    checks=compare(result['business_table'],reference)
    checks.update(no_repeated_clarification=not result['clarification']['required'],same_conversation=result['conversation_id']==session['conversation_id'],
        public_columns=len(result['business_table']['columns'])==12,
        idempotent=request('/v1/tasks',selected,selected_headers)['task_id']==task['task_id'])
    print(json.dumps({'case':'T09','checks':checks}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None)}))
        raise SystemExit(2)
