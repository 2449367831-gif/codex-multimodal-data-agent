"""Verify public continuation batches against an independent source prefix."""

import argparse
from datetime import datetime, timedelta
import json
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

from check_sales_detail_windows_live import (
    ROOT, ChatClient, CodexMcpBridge, _hide_windows_subprocesses,
    independent_control_sql, reference, validate_doris_result, verify_rows,
)


def prefix_sql(base, key):
    fields = ('owner', 'document', 'sku')
    greater = exp.or_(*(exp.and_(
        *(exp.EQ(this=exp.column(fields[j]), expression=exp.Literal.string(key[j])) for j in range(i)),
        exp.GT(this=exp.column(fields[i]), expression=exp.Literal.string(key[i])),
    ) for i in range(3)))
    predicate = exp.not_(greater)
    tree = sqlglot.parse_one(base, read='doris')
    tree = tree.where(predicate.copy())
    documents = exp.select('owner', 'document').distinct().from_('ledger').where(predicate.copy())
    count = exp.select(exp.Count(this=exp.Star())).from_(documents.subquery('prefix_documents'))
    tree.set('expressions', [exp.alias_(exp.Subquery(this=count), 'document_count')
        if column.alias_or_name == 'document_count' else column for column in tree.selects])
    return tree.sql(dialect='doris')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--max-batches', type=int, default=2)
    parser.add_argument('--monthly-partitions', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.days <= 3650 or not 1 <= args.max_batches <= 1000:
        parser.error('invalid verification bounds')
    _hide_windows_subprocesses()
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    start = today - timedelta(days=args.days)
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    base = independent_control_sql(start, today)
    def controls(sql):
        return reference(validate_doris_result(bridge.query(sql=sql, purpose='independent batch source controls')).rows)
    before = controls(base)
    client = ChatClient('http://127.0.0.1:8021')
    session = client.request('/v1/session')
    def submit(body, key):
        task = client.request('/v1/tasks', {'conversation_id': session['conversation_id'], **body}, session['csrf_token'], key)
        print(json.dumps({'task_id': task['task_id'], 'state': task['state']}), flush=True)
        began = time.monotonic()
        while task['state'] in {'queued', 'running'} and time.monotonic() - began < 300:
            time.sleep(2)
            task = client.request('/v1/tasks/' + task['task_id'])
        if task['state'] != 'completed':
            print(json.dumps({'task_id': task['task_id'], 'state': task['state'],
                'category': (task.get('result') or {}).get('error', {}).get('code')}), flush=True)
            raise RuntimeError('verification task not completed')
        return task['result']
    question = ('按月拆分查询' if args.monthly_partitions else '查询') + f'近{args.days}个完整自然日实际销售出库明细，用表格展示。'
    summary = submit({'question': question}, 'batch-live-summary-' + uuid4().hex)
    assert summary['result_state'] == 'summary_only'
    meta = summary['business_table']['detail_batches']
    assert meta['loaded_rows'] == 0 and meta['total_rows'] == before['detail_count']
    rows = []
    for _ in range(args.max_batches):
        selection = {'result_id': meta['result_id'], 'batch_index': meta['next_batch']}
        result = submit({'detail_batch': selection}, 'batch-live-part-' + uuid4().hex)
        assert result['detail_batch_source'] == selection
        table = result['business_table']
        following = table['detail_batches']
        assert following['result_id'] == meta['result_id'] and following['next_batch'] == meta['next_batch'] + 1
        assert following['total_rows'] == before['detail_count']
        pages = table.get('pagination')
        batch_rows = []
        for number in range(1, (pages['page_count'] if pages else 1) + 1):
            page = table if number == 1 else client.request(f'/v1/results/{pages["result_id"]}/pages/{number}')['business_table']
            assert all(page[key] == table[key] for key in ('caption', 'summary', 'columns', 'notes', 'detail_batches'))
            assert len(page['rows']) <= 100
            if pages:
                assert page['pagination']['page'] == number and page['pagination']['reconciled'] is True
                assert page['pagination']['start_row'] == len(batch_rows) + 1
            batch_rows.extend(page['rows'])
        assert 1 <= len(batch_rows) <= 5000 and following['loaded_rows'] == len(rows) + len(batch_rows)
        rows.extend(batch_rows)
        last = tuple(rows[-1][field] for field in ('cargo_owner', 'outbound_code', 'sku_code'))
        prefix = controls(prefix_sql(base, last))
        checks = {'independent_prefix_matches': verify_rows(rows, prefix, start=start, end=today),
            'whole_range_unchanged': controls(base) == before,
            'completion_is_explicit': result['result_state'] == ('live_success' if following['complete'] else 'batch_complete'),
            'completion_matches_original_count': following['complete'] is (len(rows) == before['detail_count']),
            'calendar_anchor_unchanged': datetime.now(ZoneInfo('Asia/Shanghai')).date() == today,
            'no_internal_sources': not any(marker in json.dumps(result).casefold() for marker in ('select ', 'ywzt_ods.', 'query_plan', 'password='))}
        replay = submit({'detail_batch': selection}, 'batch-live-replay-' + uuid4().hex)
        checks['duplicate_batch_replays_same_result'] = replay == result
        print(json.dumps({'batch': selection['batch_index'], 'state': result['result_state'], 'checks': checks}), flush=True)
        assert all(checks.values())
        meta = following
        if meta['complete']:
            break
    print(json.dumps({'verification': 'passed', 'whole_request_complete': meta['complete'],
                      'monthly_partitions': args.monthly_partitions,
                      'verified_batches': meta['next_batch'] - 1}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'verification': 'failed', 'category': type(exc).__name__}), flush=True)
        raise SystemExit(2) from None
