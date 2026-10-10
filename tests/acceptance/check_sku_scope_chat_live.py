"""Verify exact SKU scope through chat. Business rows stay in memory only."""

from datetime import datetime, timedelta
from decimal import Decimal
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


def reference_sql(start, end):
    return f"""WITH eligible AS (
 SELECT DISTINCT TRIM(h.company_code) owner, TRIM(h.outbound_code) doc
 FROM ywzt_ods.scm_sales_shipment_header h
 WHERE h.ship_at >= '{start} 00:00:00' AND h.ship_at < '{end} 00:00:00'
 AND h.status IN (700,900,701,901)
 AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%')
 AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
 AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header o
 WHERE TRIM(o.company_code)=TRIM(h.company_code) AND TRIM(o.sales_order_code)=TRIM(h.ref_order_code)
 AND TRIM(o.sales_order_code)<>'' AND COALESCE(o.deleted_flag,0)=0 AND IFNULL(o._fdl_marked_deleted,'false')='false')
), docs AS (
 SELECT e.owner,e.doc,TRIM(d.sku_code) sku,SUM(d.ship_qty) quantity
 FROM eligible e JOIN ywzt_ods.scm_sales_shipment_detail d
 ON TRIM(d.company_code)=e.owner AND TRIM(d.outbound_code)=e.doc
 WHERE TRIM(d.sku_code)='QY-YWKC500G' AND COALESCE(d.delete_flag,0)=0
 AND IFNULL(d._fdl_marked_deleted,'false')='false'
 GROUP BY e.owner,e.doc,TRIM(d.sku_code)
)
SELECT sku,SUM(quantity) quantity,COUNT(*) docs FROM docs GROUP BY sku LIMIT 2"""


def main():
    _hide_windows_subprocesses()
    base = 'http://127.0.0.1:8021'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path, body=None, headers=None):
        req = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','Origin':base,**(headers or {})})
        with opener.open(req,timeout=30) as response:
            return json.load(response)
    session = request('/v1/session')
    bridge = CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    for reference in ('`QY-YWKC500G`', 'QY-YWKC500G'):
        body = {'question':f'查询昨天 SKU {reference} 的实际销售出库数量和出库单数，按SKU汇总。','conversation_id':session['conversation_id']}
        headers = {'X-CSRF-Token':session['csrf_token'],'Idempotency-Key':'sku-scope-'+uuid4().hex}
        task = request('/v1/tasks',body,headers)
        print(json.dumps({'task_id':task['task_id'],'state':task['state']}),flush=True)
        deadline = time.monotonic()+300
        while task['state'] in {'queued','running'}:
            if time.monotonic() >= deadline:
                print(json.dumps({'task_id':task['task_id'],'state':'observation_timeout'}))
                return 2
            time.sleep(1)
            task = request('/v1/tasks/'+task['task_id'])
        result = task.get('result',{})
        if task.get('http_status') != 200 or result.get('result_state') not in {'live_success','no_data'}:
            print(json.dumps({'status':task.get('http_status'),'state':result.get('result_state'),'code':result.get('error',{}).get('code')}))
            return 1
        table = result['business_table']
        rows = validate_doris_result(bridge.query(sql=reference_sql(today-timedelta(days=1),today),purpose='exact SKU scope independent reconciliation')).rows
        actual = {r['sku_code']:(Decimal(r['quantity']),int(r['outbound_count'])) for r in table['rows']}
        expected = {r['sku']:(Decimal(str(r['quantity'])),int(r['docs'])) for r in rows}
        checks = {'exact_scope':set(actual) <= {'QY-YWKC500G'},'independently_reconciled':actual==expected,
            'unique_keys':len(actual)==len(table['rows']), 'no_clarification':not result['clarification']['required'],
            'same_conversation':result['conversation_id']==session['conversation_id'],
            'idempotent':request('/v1/tasks',body,headers)['task_id']==task['task_id']}
        print(json.dumps({'check':'quoted_sku' if reference.startswith('`') else 'plain_sku','result_state':result['result_state'],'checks':checks}),flush=True)
        if not all(checks.values()):
            return 1
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None)}))
        raise SystemExit(2)
