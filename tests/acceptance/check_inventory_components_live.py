"""Independent per-source inventory reference; never persists business rows."""

from datetime import datetime, timedelta
import argparse
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from sqlglot import exp

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.agent_core.inventory_components import InventoryComponentPlan, retrieve_inventory_components, compile_inventory_quality, QUALITY
from app.agent_core.dms_points_orders import _count
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


KEYS=('cargo_owner_id','wms_code','sku_code')
FIELDS=('in_stock_ea','stock_lock_ea','freeze_ea','move_in_ea','sales_occupation_ea','transfer_occupation_ea',
        'stock_rows','occupation_rows','negative_stock_rows','negative_stock_move_rows','missing_stock_time_rows',
        'negative_lock_rows','negative_freeze_rows','negative_move_rows',
        'negative_occupation_rows','missing_reference_rows','future_occupation_rows')
PROGRESS={'phase':'not_started','completed_queries':0}


class InventoryReferenceQualityError(ValueError):
    def __init__(self, failures):
        super().__init__('independent inventory source quality failed')
        self.quality_failures = tuple(sorted(failures))


def reference_sql(plan,source):
    since,observed=(value.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') for value in (plan.occupation_since,plan.observed_at))
    scope=[]
    for key,values in zip(KEYS,(plan.owner_codes,plan.warehouse_codes,plan.sku_codes)):
        if values:scope.append(f'TRIM(CAST({key} AS STRING)) IN ('+','.join(exp.Literal.string(v).sql(dialect='doris') for v in values)+')')
    where=' AND '.join(scope) or 'TRUE'
    key_projection=','.join(f"NULLIF(TRIM(CAST({key} AS STRING)),'') AS {key}" for key in KEYS)
    grouping=','.join(KEYS)
    if source=='stock':
        return f"""WITH observations AS (
 SELECT {key_projection},CAST(COALESCE(in_stock_quantity,0) AS DECIMAL(28,4)) AS quantity,
   TRIM(COALESCE(stock_lock,'')) AS locked,CAST(COALESCE(freeze_quantity,0) AS DECIMAL(28,4)) AS frozen,
   CAST(COALESCE(move_in_quantity,0) AS DECIMAL(28,4)) AS moving,stock_change_time
 FROM ywzt_ods.scm_warehous_manage
 WHERE (inventory_status IN ('ZP','') OR inventory_status IS NULL) AND ({where})
), reference_groups AS (
 SELECT {grouping},SUM(quantity) AS in_stock_ea,SUM(frozen) AS freeze_ea,SUM(moving) AS move_in_ea,
   SUM(CASE WHEN locked REGEXP '^-?[0-9]{{1,14}}([.][0-9]{{1,4}})?$' THEN CAST(locked AS DECIMAL(28,4)) ELSE 0 END) AS stock_lock_ea,
   SUM(CASE WHEN locked<>'' AND NOT (locked REGEXP '^-?[0-9]{{1,14}}([.][0-9]{{1,4}})?$') THEN 1 ELSE 0 END) AS invalid,
   COUNT(*) AS stock_rows,SUM(CASE WHEN quantity<0 THEN 1 ELSE 0 END) AS negative_stock_rows,
   SUM(CASE WHEN quantity+moving<0 THEN 1 ELSE 0 END) AS negative_stock_move_rows,
   SUM(CASE WHEN locked REGEXP '^-?[0-9]{{1,14}}([.][0-9]{{1,4}})?$' THEN CASE WHEN CAST(locked AS DECIMAL(28,4))<0 THEN 1 ELSE 0 END ELSE 0 END) AS negative_lock_rows,
   SUM(CASE WHEN frozen<0 THEN 1 ELSE 0 END) AS negative_freeze_rows,
   SUM(CASE WHEN moving<0 THEN 1 ELSE 0 END) AS negative_move_rows,
   SUM(CASE WHEN stock_change_time IS NULL THEN 1 ELSE 0 END) AS missing_stock_time_rows,
   MAX(stock_change_time) AS stock_latest FROM observations GROUP BY {grouping}
)
SELECT *,COUNT(*) OVER() AS group_count FROM reference_groups ORDER BY {grouping} LIMIT 501"""
    if source!='occupation':raise ValueError('unknown reference source')
    return f"""WITH observations AS (
 SELECT {key_projection},CAST(COALESCE(freeze_quantity,0) AS DECIMAL(28,4)) AS quantity,
   pre_order_code,created_date,TRIM(status) AS state
 FROM ywzt_ods.scm_warehous_occupy WHERE ({where}) AND status IS NOT NULL
   AND (created_date>'{since}' OR created_date IS NULL)
), typed AS (
 SELECT *,CASE WHEN state REGEXP '^[0-9]{{1,9}}$' THEN CAST(state AS BIGINT) ELSE NULL END AS numeric_state FROM observations
), eligible AS (
 SELECT *,CASE WHEN COALESCE(pre_order_code,'') LIKE 'HJXH-TH%' THEN 1 ELSE 0 END AS is_transfer
 FROM typed WHERE numeric_state IS NULL OR numeric_state<>1
), reference_groups AS (
 SELECT {grouping},SUM(CASE WHEN is_transfer=0 THEN quantity ELSE 0 END) AS sales_occupation_ea,
   SUM(CASE WHEN is_transfer=1 THEN quantity ELSE 0 END) AS transfer_occupation_ea,
    SUM(CASE WHEN numeric_state IS NULL OR created_date IS NULL THEN 1 ELSE 0 END) AS invalid,
    SUM(CASE WHEN numeric_state IS NULL THEN 1 ELSE 0 END) AS invalid_status,
    SUM(CASE WHEN created_date IS NULL THEN 1 ELSE 0 END) AS missing_date,
   COUNT(*) AS occupation_rows,SUM(CASE WHEN quantity<0 THEN 1 ELSE 0 END) AS negative_occupation_rows,
   SUM(CASE WHEN pre_order_code IS NULL OR pre_order_code='' THEN 1 ELSE 0 END) AS missing_reference_rows,
   SUM(CASE WHEN created_date>'{observed}' THEN 1 ELSE 0 END) AS future_occupation_rows,
   MAX(created_date) AS occupation_latest FROM eligible GROUP BY {grouping}
)
SELECT *,COUNT(*) OVER() AS group_count FROM reference_groups ORDER BY {grouping} LIMIT 501"""


def instant(value):
    if value is None:return None
    parsed=datetime.fromisoformat(str(value))
    return (parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if parsed.tzinfo is None else parsed.astimezone(ZoneInfo('Asia/Shanghai'))).isoformat()


def expected_groups(stock,occupation):
    rows={}
    failures=set()
    for source,kind in ((stock,'stock'),(occupation,'occupation')):
        seen=set()
        for row in source:
            key=tuple(row[field] for field in KEYS)
            if any(not isinstance(value,str) or not value or value!=value.strip(' ') for value in key):
                failures.add('invalid_key_count')
            if key in seen:
                failures.add('duplicate_reference_key')
            seen.add(key)
            if int(row['invalid']):
                if kind=='stock':
                    failures.add('invalid_stock_lock_count')
                else:
                    if int(row['invalid_status']):failures.add('invalid_occupation_status_count')
                    if int(row['missing_date']):failures.add('missing_occupation_date_count')
    if failures:
        raise InventoryReferenceQualityError(failures)
    for source,kind in ((stock,'stock'),(occupation,'occupation')):
        for row in source:
            key=tuple(row[field] for field in KEYS)
            if key not in rows:
                rows[key]={**dict(zip(KEYS,key)),**{name:Decimal(0) for name in FIELDS},'stock_latest':None,'occupation_latest':None}
            target=rows[key]
            for field in FIELDS:
                if field in row:target[field]=Decimal(str(row[field]))
            target[kind+'_latest']=instant(row[kind+'_latest'])
    for row in rows.values():
        row['allocation_ea']=row['sales_occupation_ea']
        row['business_occupation_ea']=fixed_decimal(Fraction(row['sales_occupation_ea'])+Fraction(row['transfer_occupation_ea']))
        row['deduction_ea']=row['stock_lock_ea'] or row['freeze_ea']
        raw=fixed_decimal(Fraction(row['in_stock_ea'])-Fraction(row['business_occupation_ea'])-Fraction(row['deduction_ea'])) if row['stock_rows'] else None
        row['raw_available_ea']=raw
        row['available_ea']=max(Decimal(0),raw) if raw is not None else None
        row['clamped']=raw is not None and raw<0
        row['available_above_stock']=raw is not None and max(Decimal(0),raw)>row['in_stock_ea']
    return [rows[key] for key in sorted(rows)]


def fixed_decimal(value):
    # Independent integer arithmetic avoids sharing the runtime Decimal context.
    ticks = value * 10000
    if ticks.denominator != 1:
        raise ValueError('reference quantity exceeds source scale')
    number = ticks.numerator
    magnitude = abs(number)
    return Decimal(f"{'-' if number < 0 else ''}{magnitude // 10000}.{magnitude % 10000:04d}")


def compare(actual,expected):
    def indexed(rows):return {tuple(row[name] for name in KEYS):row for row in rows}
    left,right=indexed(actual.groups),indexed(expected)
    return {'all_keys':left.keys()==right.keys(),'all_components_and_diagnostics':left==right,
            'all_source_totals':all(Fraction(actual.totals[name])==sum((Fraction(row[name]) for row in expected),Fraction(0)) for name in FIELDS)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--page-size',type=int,default=2000)
    parser.add_argument('--quality-only',action='store_true',help='Inspect source quality flags only; never accept the inventory business case')
    args=parser.parse_args()
    if not 1<=args.page_size<=10000:parser.error('page size must be between 1 and 10000')
    paging={'page_size':args.page_size,'page_cap':max(500,args.page_size)}
    _hide_windows_subprocesses()
    now=datetime.now(ZoneInfo('Asia/Shanghai')).replace(microsecond=0)
    plan=InventoryComponentPlan(observed_at=now,occupation_since=now-timedelta(days=60))
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    with bridge.request_session() as client:
        def read(sql,*,purpose):return validate_doris_result(client.query(sql=sql,purpose=purpose)).rows
        if args.quality_only:
            PROGRESS['phase']='quality_diagnostic_only'
            diagnostic=read(compile_inventory_quality(plan),purpose='inventory candidate quality flags only; no business quantities')
            PROGRESS['completed_queries']=1
            if len(diagnostic)!=1 or set(diagnostic[0])!=set(QUALITY):
                raise ValueError('quality diagnostic schema mismatch')
            failures=[name for name in QUALITY if _count(diagnostic[0][name])]
            print(json.dumps({'case':'inventory_components','diagnostic_only':True,'business_acceptance':False,
                              'quality_failures':failures,'progress':PROGRESS}),flush=True)
            return 0
        def many(statements,*,purpose,timeout_seconds):
            responses=client.query_many(statements=statements,purpose=purpose,timeout_seconds=timeout_seconds)
            PROGRESS['completed_queries']+=len(responses)
            return [validate_doris_result(value).rows for value in responses]
        def reference():
            PROGRESS['phase']='reference_stock'
            stock=retrieve_sales_groups(reference_sql(plan,'stock'),read,query_many=many,
                                        consistency_retries=1,**paging)
            PROGRESS['phase']='reference_occupation'
            occupied=retrieve_sales_groups(reference_sql(plan,'occupation'),read,query_many=many,
                                           consistency_retries=1,**paging)
            PROGRESS['phase']='reference_quality_and_merge'
            return expected_groups(stock,occupied)
        before=reference()
        print(json.dumps({'stage':'independent_reference_complete','scope':'ALL','progress':PROGRESS}),flush=True)
        PROGRESS['phase']='runtime_components'
        # Allow one bounded consistency retry when live inventory changes
        # between the two complete reads; persistent instability stays a hard
        # failure and is never published as a partial result.
        actual=retrieve_inventory_components(plan,read,query_many=many,
                                             consistency_retries=1,**paging)
        checks=compare(actual,before)
        checks['reference_stable']=before==reference()
    print(json.dumps({'case':'inventory_components','checks':checks,'progress':PROGRESS}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case':'inventory_components','passed':False,'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None),
                          'page_failure':getattr(exc,'page_failure',None),'pass_number':getattr(exc,'pass_number',None),
                          'quality_failures':getattr(exc,'quality_failures',None),'progress':PROGRESS}),flush=True)
        raise SystemExit(1)
