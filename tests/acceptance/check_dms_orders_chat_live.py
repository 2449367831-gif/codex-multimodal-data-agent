"""Compare all public DMS order pages with an independent governed reference."""

import http.cookiejar
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import urllib.request
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.agent_core.sales_pages import retrieve_sales_groups
from check_dms_points_orders_live import reference_sql, compare


def main():
    _hide_windows_subprocesses()
    base='http://127.0.0.1:8021'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path,body=None,headers=None):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','Origin':base,**(headers or {})})
        with opener.open(req,timeout=30) as response:return json.load(response)
    session=request('/v1/session')
    cases=json.loads((ROOT/'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))['cases']
    body={'question':next(case['question'] for case in cases if case['id']=='T25'),'conversation_id':session['conversation_id']}
    headers={'X-CSRF-Token':session['csrf_token'],'Idempotency-Key':'dms-orders-acceptance-'+uuid4().hex}
    task=request('/v1/tasks',body,headers)
    print(json.dumps({'task_id':task['task_id'],'state':task['state']}),flush=True)
    deadline=time.monotonic()+300
    while task['state'] in {'queued','running'}:
        if time.monotonic()>=deadline:
            print(json.dumps({'task_id':task['task_id'],'state':'observation_timeout'}),flush=True)
            return 2
        time.sleep(1)
        task=request('/v1/tasks/'+task['task_id'])
    payload=task.get('result',{})
    if task.get('http_status')!=200 or payload.get('result_state') not in {'live_success','no_data'}:
        print(json.dumps({'state':task['state'],'result_state':payload.get('result_state'),'error_code':payload.get('error',{}).get('code')}),flush=True)
        return 1
    assert len(payload['business_tables'])==2 and payload['business_table']==payload['business_tables'][0]
    complete=[]
    for table, keys in zip(payload['business_tables'],[{'month','order_count'},{'sales_order_code','customer_code','customer_name','created_time'}]):
        assert {column['key'] for column in table['columns']}==keys
        paging=table.get('pagination')
        rows=[]
        for number in range(1,paging['page_count']+1 if paging else 2):
            page=request(f"/v1/results/{paging['result_id']}/pages/{number}")['business_table'] if paging else table
            assert {column['key'] for column in page['columns']}==keys
            assert all(set(row)==keys for row in page['rows'])
            if paging:
                meta=page['pagination']
                assert meta['page']==number and meta['start_row']==len(rows)+1 and meta['result_id']==paging['result_id']
                assert meta['total_rows']==paging['total_rows'] and meta['page_count']==paging['page_count'] and meta['reconciled']
                assert meta['end_row']==len(rows)+len(page['rows'])
            rows.extend(page['rows'])
        assert not paging or len(rows)==paging['total_rows']
        complete.append(rows)
    months,orders=complete
    result=SimpleNamespace(orders=orders,months=[{'month':r['month'],'order_count':int(r['order_count'])} for r in months],
        order_count=len(orders),missing_customer_code_count=sum(r['customer_code'] is None for r in orders),
        missing_customer_name_count=sum(r['customer_name'] is None for r in orders))
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    def read(sql,*,purpose):return validate_doris_result(bridge.query(sql=sql,purpose=purpose)).rows
    reference=retrieve_sales_groups(reference_sql(),read)
    checks=compare(result,reference)
    repeat=request('/v1/tasks',body,headers)
    checks.update(public_fields=True,all_pages=True,no_clarification=not payload['clarification']['required'],
        replay_same_result=repeat['task_id']==task['task_id'] and repeat['result']['business_tables']==payload['business_tables'],
        summary_count=int(payload['business_tables'][0]['summary'][0]['value'])==len(orders))
    print(json.dumps({'case':'T25_chat','checks':checks}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None)}),flush=True)
        raise SystemExit(2)
