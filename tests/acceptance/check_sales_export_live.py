"""Consume complete public CSV chunks in memory and reconcile an independent ledger."""

import argparse
import csv
from datetime import datetime, timedelta
from fractions import Fraction
from hashlib import sha256
import io
import json
import time
from urllib.request import Request
from urllib.error import HTTPError
from uuid import uuid4
from zoneinfo import ZoneInfo

from check_sales_detail_batches_live import prefix_sql
from check_sales_detail_windows_live import (
    ROOT, ChatClient, CodexMcpBridge, _hide_windows_subprocesses,
    independent_control_sql, reference, validate_doris_result,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--max-chunks', type=int, default=100)
    parser.add_argument('--resume-export', help='existing owner-bound export ID; reread and verify every chunk')
    args = parser.parse_args()
    if not 1 <= args.days <= 3650 or not 1 <= args.max_chunks <= 2000:
        parser.error('invalid verification bounds')
    _hide_windows_subprocesses()
    end = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    start = end - timedelta(days=args.days)
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    base = independent_control_sql(start, end)
    def controls(statement):
        return reference(validate_doris_result(bridge.query(sql=statement, purpose='independent export control')).rows)
    before = controls(base)
    client = ChatClient('http://127.0.0.1:8021')
    session = client.request('/v1/session')
    if args.resume_export:
        import re
        assert re.fullmatch(r'ex_[a-f0-9]{32}', args.resume_export)
        identifier = args.resume_export
    else:
        task = client.request('/v1/tasks', {'conversation_id':session['conversation_id'],
            'question':f'查询近{args.days}个完整自然日实际销售出库明细，用表格展示。'}, session['csrf_token'], 'export-live-'+uuid4().hex)
        print(json.dumps({'task_id':task['task_id'],'state':task['state']}), flush=True)
        started = time.monotonic()
        while task['state'] in {'queued','running'} and time.monotonic()-started < 300:
            time.sleep(2)
            task = client.request('/v1/tasks/'+task['task_id'])
        assert task['state'] == 'completed'
        identifier = task['result']['business_table']['detail_batches']['export_id']
    meta = client.request('/v1/exports/'+identifier)
    assert meta['total_rows'] == before['detail_count']
    print(json.dumps({'export_id':identifier,'chunks':meta['chunks'],'total_rows':meta['total_rows']}),flush=True)
    loaded, documents, quantity, last = 0, 0, Fraction(0), None
    for index in range(min(args.max_chunks, meta['chunks'])):
        began = time.monotonic()
        request = Request(client.base+f'/v1/exports/{identifier}/chunks/{index}', data=b'',
            headers={'Origin':client.base,'X-CSRF-Token':session['csrf_token']}, method='POST')
        with client.opener.open(request, timeout=240) as response:
            data = response.read(32*1024*1024+1)
            headers = response.headers
        assert len(data) <= 32*1024*1024 and sha256(data).hexdigest() == headers['X-Chunk-SHA256']
        assert headers['X-Export-Id'] == identifier and headers['X-Chunk-Index'] == str(index)
        rows = csv.reader(io.StringIO(data.decode('utf-8-sig')))
        if not index:
            assert len(next(rows)) == 6
        count = 0
        for row in rows:
            assert len(row) == 6
            key = (row[0],row[2],row[3])
            assert last is None or key > last
            assert start <= datetime.fromisoformat(row[4]).date() < end
            documents += int(last is None or key[:2] != last[:2])
            quantity += Fraction(row[5])
            loaded += 1; count += 1; last = key
        assert count == int(headers['X-Chunk-Rows']) and loaded == int(headers['X-Loaded-Rows'])
        prefix = controls(prefix_sql(base,last))
        checks = {'prefix_counts':loaded==prefix['detail_count'] and documents==prefix['document_count'],
                  'prefix_quantity':quantity==Fraction(prefix['quantity']),
                  'completion_truthful':(headers['X-Export-Complete']=='true')==(loaded==before['detail_count'])}
        print(json.dumps({'chunk':index,'rows':count,'elapsed':round(time.monotonic()-began,2),'checks':checks}),flush=True)
        assert all(checks.values())
    complete = loaded == before['detail_count']
    assert controls(base) == before
    if complete:
        assert documents == before['document_count'] and quantity == Fraction(before['quantity'])
    print(json.dumps({'verification':'passed','whole_request_complete':complete,'rows_verified':loaded}),flush=True)


if __name__ == '__main__':
    try:
        main()
    except HTTPError as exc:
        try:
            category = json.load(exc).get('error',{}).get('code','http_error')
        except Exception:
            category = 'http_error'
        print(json.dumps({'verification':'failed','status':exc.code,'category':category}),flush=True)
        raise SystemExit(2) from None
    except Exception as exc:
        print(json.dumps({'verification':'failed','category':type(exc).__name__}),flush=True)
        raise SystemExit(2) from None
