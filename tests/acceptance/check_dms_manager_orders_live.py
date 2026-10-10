"""Independent T26 order/version and employee reference; no business row files."""

import argparse
from collections import defaultdict
from contextlib import nullcontext
from datetime import date, datetime
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.agent_core.dms_manager_orders import DmsManagerOrderPlan, retrieve_manager_orders, retrieve_manager_groups
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

PAGE_SPEC=spec_from_file_location('page_diagnostics',Path(__file__).with_name('page_read_diagnostics.py'))
page_diagnostics=module_from_spec(PAGE_SPEC)
PAGE_SPEC.loader.exec_module(page_diagnostics)

SPEC=spec_from_file_location('manager_reference',Path(__file__).with_name('check_dms_manager_live.py'))
manager_reference=module_from_spec(SPEC)
SPEC.loader.exec_module(manager_reference)
PROGRESS={'phase':'not_started','completed_queries':0,'query_seconds':0,'query_batches':0,'max_batch_width':0}


def reference_sql(plan):
    predicates=[]
    if plan.start is not None:
        predicates.append(f"(created_time IS NULL OR (created_time >= '{plan.start.isoformat()}' AND created_time < '{plan.end.isoformat()}'))")
    for field,values in [('customer_code',plan.customer_codes),('sales_order_code',plan.order_codes)]:
        if values:
            predicates.append('TRIM('+field+') IN ('+','.join(exp.Literal.string(value).sql(dialect='doris') for value in values)+')')
    condition=' AND '.join(predicates) or 'TRUE'
    fields=('sales_order_code','customer_code','customer_name','created_time','updated_time','version','id',
            'deleted_flag','_fdl_marked_deleted','owner_manager','province_department_name')
    if not predicates:
        return f"""WITH observations AS (
 SELECT DISTINCT {','.join(fields)} FROM dms_ods.t_sales_order
)
SELECT *,COUNT(*) OVER() AS group_count FROM observations ORDER BY {','.join(fields)} LIMIT 501"""
    return f"""WITH relevant_keys AS (
 SELECT DISTINCT NULLIF(TRIM(sales_order_code),'') AS order_key FROM dms_ods.t_sales_order WHERE {condition}
), observations AS (
 SELECT DISTINCT {','.join('o.'+field for field in fields)} FROM dms_ods.t_sales_order o
 WHERE EXISTS (SELECT 1 FROM relevant_keys k WHERE k.order_key <=> NULLIF(TRIM(o.sales_order_code),''))
)
SELECT *,COUNT(*) OVER() AS group_count FROM observations ORDER BY {','.join(fields)} LIMIT 501"""


def text(value):
    return value.strip(' ') or None if value is not None else None


def instant(value):
    if value is None:
        return None
    result=datetime.fromisoformat(str(value))
    return result.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if result.tzinfo is None else result.astimezone(ZoneInfo('Asia/Shanghai'))


def reference_headers(observations,plan):
    by_order=defaultdict(list)
    for row in observations:
        by_order[text(row['sales_order_code'])].append(row)
    def candidate(row):
        created=instant(row['created_time'])
        return (row['deleted_flag']!=1 and (row['_fdl_marked_deleted'] or '').strip(' ').lower() not in ('true','1')
                and (not plan.customer_codes or text(row['customer_code']) in plan.customer_codes)
                and (not plan.order_codes or text(row['sales_order_code']) in plan.order_codes)
                and (created is None or plan.start is None or plan.start<=created.date()<plan.end))
    def rank(row):
        updated=instant(row['updated_time'])
        return (updated is not None,updated,row['version'] is not None,row['version'] or 0,row['id'])
    result=[]
    for code,rows in by_order.items():
        if code is None:
            if any(candidate(row) for row in rows):raise ValueError('reference blank order identity')
            continue
        highest=max(rank(row) for row in rows)
        current=[row for row in rows if rank(row)==highest]
        if not any(candidate(row) for row in current):continue
        tuples={(text(row['customer_code']),text(row['customer_name']),instant(row['created_time']),instant(row['updated_time']),
                 row['deleted_flag'],(row['_fdl_marked_deleted'] or '').strip(' ').lower(),row['owner_manager'],text(row['province_department_name'])) for row in current}
        if len(tuples)!=1:raise ValueError('reference conflicting current order')
        customer,name,created,updated,deleted,cdc,manager,province=next(iter(tuples))
        if deleted not in (None,0) or cdc not in ('','false','0') or created is None:
            raise ValueError('reference invalid date or deletion')
        result.append({'sales_order_code':code,'customer_code':customer,'customer_name':name,
                       'created_time':created.strftime('%Y-%m-%d %H:%M:%S'), 'updated_time':updated,
                       'province':province,'manager_reference':manager})
    return sorted(result,key=lambda row:row['sales_order_code'])


def expected_report(headers,employee_observations):
    refs=tuple(dict.fromkeys(row['manager_reference'] for row in headers))
    managers={row['manager_reference']:row for row in manager_reference.expected_lookup(employee_observations,refs)}
    groups,orders={},[]
    for header in headers:
        manager=managers[header['manager_reference']]
        row={key:value for key,value in header.items() if key!='updated_time'}
        row.update(employee_id=manager['employee_id'],manager_name=manager['manager_name'],manager_state=manager['match_state'],
                   manager_key=manager['employee_id'] or text(header['manager_reference']),order_date=header['created_time'][:10])
        orders.append(row)
        keys=('order_date','province','manager_key','manager_state','customer_code','customer_name')
        key=tuple(row[field] for field in keys)
        if key not in groups:
            groups[key]={field:row[field] for field in keys}
            groups[key].update(manager_name=row['manager_name'],order_count=0,missing_manager_order_count=0)
        groups[key]['order_count']+=1
        groups[key]['missing_manager_order_count']+=row['manager_state']!='matched'
    updated=[row['updated_time'] for row in headers if row['updated_time'] is not None]
    return {'orders':orders,'groups':list(groups.values()),'order_count':len(headers),
            'missing_manager_order_count':sum(row['manager_state']!='matched' for row in orders),
            'missing_province_order_count':sum(row['province'] is None for row in orders),
            'missing_customer_code_count':sum(row['customer_code'] is None for row in orders),
            'missing_customer_name_count':sum(row['customer_name'] is None for row in orders),
            'missing_updated_count':len(headers)-len(updated),
            'source_latest_updated':max(updated).strftime('%Y-%m-%d %H:%M:%S') if updated else None}


def compare(actual,expected):
    def canonical(rows):
        return sorted(json.dumps(row,sort_keys=True,ensure_ascii=True) for row in rows)
    return {'order_identity_and_attributes':canonical(actual.orders)==canonical(expected['orders']),
            'all_group_keys_labels_and_counts':canonical(actual.groups)==canonical(expected['groups']),
            'missing_attribute_and_freshness_controls':all(getattr(actual,key)==value for key,value in expected.items() if key not in ('orders','groups'))}


def reference_group_sql(plan):
    observations=sqlglot.parse_one(reference_sql(plan),read='doris')
    ctes=','.join(cte.sql(dialect='doris') for cte in observations.args['with_'].expressions)
    predicates=[]
    if plan.start is not None:
        predicates.append(f"(created_time IS NULL OR (created_time >= '{plan.start.isoformat()}' AND created_time < '{plan.end.isoformat()}'))")
    for field,values in [('customer_code',plan.customer_codes),('sales_order_code',plan.order_codes)]:
        if values:predicates.append(field+' IN ('+','.join(exp.Literal.string(value).sql(dialect='doris') for value in values)+')')
    scope=' AND '.join(predicates) or 'TRUE'
    alive=f"COALESCE(deleted_flag,0)<>1 AND cdc NOT IN ('true','1') AND ({scope})"
    valid="created_time IS NOT NULL AND COALESCE(deleted_flag,0)=0 AND cdc IN ('','false','0')"
    fields=('order_date','province','manager_reference','customer_code','customer_name')
    return f"""WITH {ctes}, normalized AS (
 SELECT NULLIF(TRIM(sales_order_code),'') AS sales_order_code,NULLIF(TRIM(customer_code),'') AS customer_code,
   NULLIF(TRIM(customer_name),'') AS customer_name,created_time,updated_time,version,id,deleted_flag,
   LOWER(TRIM(COALESCE(_fdl_marked_deleted,''))) AS cdc,owner_manager AS manager_reference,
   NULLIF(TRIM(province_department_name),'') AS province FROM observations
), numbered AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY sales_order_code ORDER BY updated_time DESC NULLS LAST,version DESC NULLS LAST,id DESC) AS ordinal
 FROM normalized WHERE sales_order_code IS NOT NULL
), latest_observations AS (
 SELECT DISTINCT n.sales_order_code,n.customer_code,n.customer_name,n.created_time,n.updated_time,
   n.deleted_flag,n.cdc,n.manager_reference,n.province
 FROM normalized n JOIN numbered winner ON n.sales_order_code=winner.sales_order_code
   AND n.updated_time <=> winner.updated_time AND n.version <=> winner.version AND n.id <=> winner.id
 WHERE winner.ordinal=1
), version_counts AS (
 SELECT sales_order_code,COUNT(*) AS variants FROM latest_observations GROUP BY sales_order_code
), scoped_observations AS (
 SELECT * FROM latest_observations WHERE {alive}
), header_groups AS (
 SELECT DATE_FORMAT(created_time,'%Y-%m-%d') AS order_date,province,manager_reference,customer_code,customer_name,
   COUNT(DISTINCT sales_order_code) AS order_count,
   SUM(CASE WHEN updated_time IS NULL THEN 1 ELSE 0 END) AS missing_updated_count,
   MAX(updated_time) AS latest_updated FROM scoped_observations WHERE {valid}
 GROUP BY DATE_FORMAT(created_time,'%Y-%m-%d'),province,manager_reference,customer_code,customer_name
), reference_quality AS (
 SELECT (SELECT COUNT(*) FROM normalized WHERE sales_order_code IS NULL AND {alive}) AS blank_count,
   (SELECT COUNT(*) FROM scoped_observations WHERE created_time IS NULL) AS missing_date_count,
   (SELECT COUNT(*) FROM scoped_observations s JOIN version_counts v ON s.sales_order_code=v.sales_order_code WHERE v.variants>1) AS conflict_count,
   (SELECT COUNT(*) FROM scoped_observations WHERE cdc NOT IN ('','false','0') OR COALESCE(deleted_flag,0) NOT IN (0,1)) AS invalid_delete_count,
   (SELECT COUNT(DISTINCT sales_order_code) FROM scoped_observations WHERE {valid}) AS total_order_count
), reference_report AS (
 SELECT 'group' AS row_kind,h.* FROM header_groups h
 UNION ALL SELECT 'control',CAST(NULL AS STRING),NULL,NULL,NULL,NULL,0,0,CAST(NULL AS DATETIME)
)
SELECT r.*,q.*,1+(SELECT COUNT(*) FROM header_groups) AS group_count
FROM reference_report r CROSS JOIN reference_quality q
ORDER BY row_kind,{','.join(fields)} LIMIT 501"""


def check_reference_groups(rows):
    controls=('blank_count','missing_date_count','conflict_count','invalid_delete_count','total_order_count','group_count')
    if not rows or int(rows[0]['group_count'])!=len(rows):raise ValueError('incomplete grouped reference')
    first={key:int(rows[0][key]) for key in controls}
    if any(first[key] for key in controls[:4]):raise ValueError('reference order quality')
    groups=[]
    seen=set()
    control_count=0
    for row in rows:
        if any(int(row[key])!=first[key] for key in controls):raise ValueError('changed reference controls')
        if row['row_kind']=='control':
            control_count+=1
            if row['order_count']!=0:raise ValueError('invalid reference control')
            continue
        if row['row_kind']!='group' or int(row['order_count'])<=0:raise ValueError('invalid reference group')
        key=tuple(row[key] for key in ('order_date','province','manager_reference','customer_code','customer_name'))
        if key in seen:raise ValueError('duplicate reference group')
        seen.add(key)
        groups.append(row)
    if control_count!=1 or sum(int(row['order_count']) for row in groups)!=first['total_order_count']:
        raise ValueError('reference grouped and distinct order counts disagree')
    return groups


def expected_group_report(header_groups,employee_observations):
    refs=tuple(dict.fromkeys(row['manager_reference'] for row in header_groups))
    managers={row['manager_reference']:row for row in manager_reference.expected_lookup(employee_observations,refs)}
    groups={}
    result={key:0 for key in ('order_count','missing_manager_order_count','missing_province_order_count',
                              'missing_customer_code_count','missing_customer_name_count','missing_updated_count')}
    latest=None
    for header in header_groups:
        manager=managers[header['manager_reference']]
        count=int(header['order_count'])
        key=(header['order_date'],header['province'],manager['employee_id'] or text(header['manager_reference']),
             manager['match_state'],header['customer_code'],header['customer_name'])
        if key not in groups:
            groups[key]=dict(zip(('order_date','province','manager_key','manager_state','customer_code','customer_name'),key))
            groups[key].update(manager_name=manager['manager_name'],order_count=0,missing_manager_order_count=0)
        groups[key]['order_count']+=count
        missing=count if manager['match_state']!='matched' else 0
        groups[key]['missing_manager_order_count']+=missing
        result['order_count']+=count
        result['missing_manager_order_count']+=missing
        for field,target in [('province','missing_province_order_count'),('customer_code','missing_customer_code_count'),('customer_name','missing_customer_name_count')]:
            result[target]+=count if header[field] is None else 0
        result['missing_updated_count']+=int(header['missing_updated_count'])
        updated=instant(header['latest_updated'])
        if updated is not None:latest=max(latest,updated) if latest is not None else updated
    result['source_latest_updated']=latest.strftime('%Y-%m-%d %H:%M:%S') if latest else None
    result['groups']=list(groups.values())
    return result


def compare_groups(actual,expected):
    def canonical(rows):return sorted(json.dumps(row,sort_keys=True,ensure_ascii=True) for row in rows)
    return {'all_group_keys_labels_and_counts':canonical(actual.groups)==canonical(expected['groups']),
            'order_and_missing_attribute_controls':all(getattr(actual,key)==value for key,value in expected.items() if key!='groups')}


def integrity_failure(exc):
    known={
        'source changed between aggregate reads':'content_changed_between_passes',
        'missing aggregate batch':'missing_batch',
        'missing aggregate page':'missing_page',
        'aggregate page ordinal changed':'page_ordinal_changed',
        'aggregate page count changed':'page_count_changed',
    }
    while exc is not None:
        if type(exc) is ValueError and str(exc) in known:
            return known[str(exc)]
        exc=exc.__cause__
    return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--start',type=date.fromisoformat)
    parser.add_argument('--end',type=date.fromisoformat)
    parser.add_argument('--grouped',action='store_true')
    parser.add_argument('--one-shot',action='store_true')
    parser.add_argument('--sequential',action='store_true')
    parser.add_argument('--page-size',type=int,default=500)
    parser.add_argument('--diagnose-pages',action='store_true')
    args=parser.parse_args()
    if not 1<=args.page_size<=10000:
        parser.error('--page-size must be between 1 and 10000; backend caps still apply')
    paging={'page_size':args.page_size,'page_cap':max(500,args.page_size)}
    PROGRESS['page_size']=args.page_size
    plan=DmsManagerOrderPlan(start=args.start,end=args.end)
    _hide_windows_subprocesses()
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    diagnostic=None
    def observed(sql,rows):
        if diagnostic is not None:
            tree=sqlglot.parse_one(sql,read='doris')
            boundary=next(node for node in tree.find_all(exp.GT) if isinstance(node.this,exp.Column) and node.this.name=='result_ordinal')
            diagnostic.observe(int(boundary.expression.this),rows)
            PROGRESS['page_diagnostics']=diagnostic.summary()
        return rows
    def query(sql,*,purpose):
        import time
        started=time.monotonic()
        try:
            rows=validate_doris_result(query_client.query(sql=sql,purpose=purpose)).rows
            PROGRESS['completed_queries']+=1
            return observed(sql,rows)
        finally:
            PROGRESS['query_seconds']=round(PROGRESS['query_seconds']+time.monotonic()-started,2)
    def query_many(statements,*,purpose,timeout_seconds):
        import time
        started=time.monotonic()
        PROGRESS['query_batches']+=1
        PROGRESS['max_batch_width']=max(PROGRESS['max_batch_width'],len(statements))
        try:
            results=query_client.query_many(statements=statements,purpose=purpose,timeout_seconds=timeout_seconds)
            batches=[validate_doris_result(result).rows for result in results]
            PROGRESS['completed_queries']+=len(batches)
            return [observed(sql,rows) for sql,rows in zip(statements,batches)]
        finally:
            PROGRESS['query_seconds']=round(PROGRESS['query_seconds']+time.monotonic()-started,2)
    batch=query_many if not args.one_shot and not args.sequential else None
    def reference():
        nonlocal diagnostic
        PROGRESS['phase']='reference_order_groups' if args.grouped else 'reference_order_evidence'
        if args.grouped:
            if args.diagnose_pages:
                diagnostic=page_diagnostics.PageReadDiagnostics(
                    fields=('row_kind','order_date','province','manager_reference','customer_code','customer_name',
                            'order_count','missing_updated_count','latest_updated','blank_count','missing_date_count',
                            'conflict_count','invalid_delete_count','total_order_count','group_count'),
                    keys=('row_kind','order_date','province','manager_reference','customer_code','customer_name'))
            headers=check_reference_groups(retrieve_sales_groups(reference_group_sql(plan),query,query_many=batch,**paging))
            diagnostic=None
            refs=tuple(dict.fromkeys(row['manager_reference'] for row in headers))
            PROGRESS['phase']='reference_employee_groups'
            employees=retrieve_sales_groups(manager_reference.reference_sql(refs),query,query_many=batch,**paging) if refs else []
            return expected_group_report(headers,employees)
        headers=reference_headers(retrieve_sales_groups(reference_sql(plan),query,query_many=batch,**paging),plan)
        refs=tuple(dict.fromkeys(row['manager_reference'] for row in headers))
        employees=retrieve_sales_groups(manager_reference.reference_sql(refs),query,query_many=batch,**paging) if refs else []
        return expected_report(headers,employees)
    with nullcontext(bridge) if args.one_shot else bridge.request_session() as query_client:
        before=reference()
        print(json.dumps({'stage':'independent_reference_complete','all_dates':plan.start is None,'progress':PROGRESS}),flush=True)
        PROGRESS['phase']='runtime_grouped_orders' if args.grouped else 'runtime_order_evidence'
        if args.diagnose_pages and args.grouped:
            diagnostic=page_diagnostics.PageReadDiagnostics(
                fields=('row_kind','order_date','province','manager_key','manager_state','customer_code','customer_name',
                        'manager_name','variant_count','source_rows','source_updated','order_count',
                        'group_missing_updated','group_latest_updated','total_order_count','group_count',
                        'missing_manager_order_count','missing_province_order_count','missing_customer_code_count',
                        'missing_customer_name_count','missing_updated_count','blank_order_count','missing_created_count',
                        'version_conflict_count','invalid_delete_count','source_latest_updated'),
                keys=('row_kind','order_date','province','manager_key','manager_state','customer_code','customer_name'))
        actual=retrieve_manager_groups(plan,query,query_many=batch,**paging) if args.grouped else retrieve_manager_orders(plan,query,query_many=batch,**paging)
        diagnostic=None
        checks=compare_groups(actual,before) if args.grouped else compare(actual,before)
        print(json.dumps({'stage':'runtime_comparison_complete','checks':checks,'progress':PROGRESS}),flush=True)
        checks['independent_reference_stable']=before==reference()
    print(json.dumps({'case':'T26_order_core','all_dates':plan.start is None,'checks':checks,'progress':PROGRESS}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case':'T26_order_core','passed':False,'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None),'integrity_failure':integrity_failure(exc),'progress':PROGRESS}),flush=True)
        raise SystemExit(1)
