"""Independent T38 ranking and every detail check, without persisting source rows."""

from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.agent_core.sales_drilldown import DRILLDOWN_PAGE_SIZE, SalesDrilldownPlan, retrieve_sales_ranking, retrieve_ranked_sku
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.query_scope import QueryScope

PROGRESS={'phase':'not_started','completed_queries':0,'completed_skus':0}


def timed_phase(name, action):
    PROGRESS['phase'] = name
    started = time.monotonic()
    queries = PROGRESS['completed_queries']
    print(json.dumps({'stage': 'phase_started', 'progress': PROGRESS}), flush=True)
    try:
        return action()
    finally:
        print(json.dumps({'stage': 'phase_finished', 'phase': name,
                          'elapsed_seconds': round(time.monotonic()-started, 2),
                          'queries': PROGRESS['completed_queries']-queries,
                          'progress': PROGRESS}), flush=True)


def reference_sql(plan,sku_code=None):
    if plan.scope!=QueryScope(finished_goods=True):
        raise ValueError('reference requires the original ALL-owner/warehouse T38 scope')
    selected_filter='' if sku_code is None else ' AND r.sku_code='+exp.Literal.string(sku_code).sql(dialect='doris')
    return f"""WITH document_headers AS (
 SELECT DISTINCT TRIM(company_code) AS owner,TRIM(outbound_code) AS outbound_code,
   TRIM(facility_code) AS warehouse_code,NULLIF(TRIM(store_code),'') AS customer_code,ship_at
 FROM ywzt_ods.scm_sales_shipment_header h
 WHERE ship_at>='{plan.start.isoformat()} 00:00:00' AND ship_at<'{plan.end.isoformat()} 00:00:00'
   AND status IN (700,900,701,901) AND COALESCE(delete_flag,0)=0
   AND IFNULL(_fdl_marked_deleted,'false')='false'
   AND (TRIM(ref_order_type) IN ('JYCK','HHCK','BFCK','QTCK') OR TRIM(ref_order_type) LIKE 'SO%')
   AND EXISTS(SELECT 1 FROM ywzt_ods.scm_sales_order_header o
     WHERE TRIM(o.company_code)=TRIM(h.company_code) AND TRIM(o.sales_order_code)=TRIM(h.ref_order_code)
       AND TRIM(o.sales_order_code)<>'' AND COALESCE(o.deleted_flag,0)=0 AND IFNULL(o._fdl_marked_deleted,'false')='false')
), detail_documents AS (
 SELECT TRIM(d.company_code) AS owner,TRIM(d.outbound_code) AS outbound_code,TRIM(d.sku_code) AS sku_code,SUM(d.ship_qty) AS quantity
 FROM ywzt_ods.scm_sales_shipment_detail d
 WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
   AND EXISTS(SELECT 1 FROM document_headers h WHERE h.owner=TRIM(d.company_code) AND h.outbound_code=TRIM(d.outbound_code))
   AND EXISTS(SELECT 1 FROM ywzt_ods.master_sku m WHERE TRIM(m.sku_code)=TRIM(d.sku_code) AND TRIM(m.sku_group)='50')
 GROUP BY TRIM(d.company_code),TRIM(d.outbound_code),TRIM(d.sku_code)
), sku_summary AS (
 SELECT sku_code,SUM(quantity) AS sku_quantity,COUNT(*) AS outbound_count FROM detail_documents GROUP BY sku_code
), ranked AS (
 SELECT *,ROW_NUMBER() OVER(ORDER BY sku_quantity DESC,sku_code) AS rank FROM sku_summary
), product_labels AS (
 SELECT DISTINCT TRIM(sku_code) AS sku_code,NULLIF(TRIM(sku_name),'') AS sku_name
 FROM ywzt_ods.master_sku
), warehouse_labels AS (
 SELECT DISTINCT TRIM(CAST(cargo_owner_id AS STRING)) AS owner,TRIM(wms_code) AS warehouse_code,
   TRIM(wms_desc) AS warehouse_name
 FROM ywzt_ods.master_wms
 WHERE COALESCE(TRIM(CAST(status AS STRING)),'0')='0' AND NULLIF(TRIM(wms_desc),'') IS NOT NULL
), selected AS (
 SELECT d.*,h.warehouse_code,h.customer_code,h.ship_at,r.rank,r.sku_quantity,r.outbound_count,
   COALESCE(p.sku_name,d.sku_code) AS sku_name,COALESCE(w.warehouse_name,h.warehouse_code) AS warehouse_name
 FROM detail_documents d JOIN document_headers h ON h.owner=d.owner AND h.outbound_code=d.outbound_code
 JOIN ranked r ON r.sku_code=d.sku_code
 LEFT JOIN product_labels p ON p.sku_code=d.sku_code
 LEFT JOIN warehouse_labels w ON w.owner=h.owner AND w.warehouse_code=h.warehouse_code
 WHERE r.rank<={plan.top_n}{selected_filter}
)
SELECT *,COUNT(*) OVER() AS group_count,
 (SELECT SUM(quantity) FROM detail_documents) AS all_quantity,
 (SELECT COUNT(*) FROM (SELECT DISTINCT owner,outbound_code FROM detail_documents) docs) AS all_documents
FROM selected ORDER BY rank,owner,outbound_code LIMIT 501"""


def reference_ranking_sql(plan):
    tree=sqlglot.parse_one(reference_sql(plan),read='doris')
    body=sqlglot.parse_one("""SELECT r.sku_code,r.rank,r.sku_quantity,r.outbound_count,COALESCE(p.sku_name,r.sku_code) AS sku_name,
 (SELECT SUM(quantity) FROM detail_documents) AS all_quantity,
 (SELECT COUNT(*) FROM (SELECT DISTINCT owner,outbound_code FROM detail_documents) docs) AS all_documents,
 COUNT(*) OVER() AS group_count FROM ranked r LEFT JOIN product_labels p ON p.sku_code=r.sku_code
 WHERE rank<=100 ORDER BY rank LIMIT 101""",read='doris')
    body.set('with_',tree.args['with_'].copy())
    body.where(exp.LTE(this=exp.column('rank'),expression=exp.Literal.number(plan.top_n)),append=False,copy=False)
    return body.sql(dialect='doris')


def compare_ranking(snapshot,reference):
    return {'ranking_and_distinct_counts':
        [(r.rank,r.sku_code,r.quantity,r.outbound_count) for r in snapshot.ranking]==
        [(int(r['rank']),r['sku_code'],Decimal(str(r['sku_quantity'])),int(r['outbound_count'])) for r in reference],
        'ranking_display_names':[(r.rank,r.sku_code,r.sku_name) for r in snapshot.ranking]==
        [(int(r['rank']),r['sku_code'],r['sku_name']) for r in reference],
        'all_sales_quantity':snapshot.total_quantity==(Decimal(str(reference[0]['all_quantity'])) if reference else Decimal(0)),
        'all_sales_documents':snapshot.total_outbound_count==(int(reference[0]['all_documents']) if reference else 0)}


def compare(snapshot,reference):
    expected={}
    expected_names={}
    groups={}
    def instant(value):
        parsed=datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if parsed.tzinfo is None else parsed.astimezone(ZoneInfo('Asia/Shanghai'))
    for row in reference:
        key=(row['sku_code'],row['owner'],row['outbound_code'])
        if key in expected:
            raise ValueError('independent reference contains duplicate detail keys')
        expected[key]=(row['warehouse_code'],row['customer_code'],instant(row['ship_at']),Decimal(str(row['quantity'])))
        expected_names[key]=(row['sku_name'],row['warehouse_name'])
        group=(int(row['rank']),Decimal(str(row['sku_quantity'])),int(row['outbound_count']))
        if row['sku_code'] in groups and groups[row['sku_code']]!=group:
            raise ValueError('independent reference contains inconsistent ranks')
        groups[row['sku_code']]=group
    actual={(r.sku_code,r.cargo_owner,r.outbound_code):(r.warehouse_code,r.customer_code,r.ship_at,r.quantity) for r in snapshot.details}
    ranked={r.sku_code:(r.rank,r.quantity,r.outbound_count) for r in snapshot.ranking}
    product_names={r.sku_code:r.sku_name for r in snapshot.ranking}
    actual_names={(r.sku_code,r.cargo_owner,r.outbound_code):(product_names[r.sku_code],r.warehouse_name) for r in snapshot.details}
    return {'ranking_and_distinct_counts':ranked==groups,'all_drilldown_keys':actual.keys()==expected.keys(),
            'every_detail_field':actual==expected,
            'every_display_name':actual_names==expected_names,
            'sku_display_names':{key:value[0] for key,value in actual_names.items()}=={key:value[0] for key,value in expected_names.items()},
            'warehouse_display_names':{key:value[1] for key,value in actual_names.items()}=={key:value[1] for key,value in expected_names.items()},
            'all_sales_quantity':snapshot.total_quantity==(Decimal(str(reference[0]['all_quantity'])) if reference else Decimal(0)),
            'all_sales_documents':snapshot.total_outbound_count==(int(reference[0]['all_documents']) if reference else 0),
            'every_sku_drilldown_reconciles':all(sum((r.quantity for r in snapshot.drilldown(s.sku_code)),Decimal(0))==s.quantity for s in snapshot.ranking)}


def main():
    _hide_windows_subprocesses()
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
    plan=SalesDrilldownPlan(today-timedelta(days=1),today)
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    paging={'page_size':2000,'page_cap':2000}
    runtime_paging={'page_size':DRILLDOWN_PAGE_SIZE,'page_cap':DRILLDOWN_PAGE_SIZE}
    with bridge.request_session() as client:
        def read(sql,*,purpose):
            result=validate_doris_result(client.query(sql=sql,purpose=purpose)).rows
            PROGRESS['completed_queries']+=1
            return result
        def many(statements,*,purpose,timeout_seconds):
            responses=client.query_many(statements=statements,purpose=purpose,timeout_seconds=timeout_seconds)
            PROGRESS['completed_queries']+=len(responses)
            return [validate_doris_result(response).rows for response in responses]
        ranking=timed_phase('runtime_ranking', lambda: retrieve_sales_ranking(plan,read,query_many=many,**runtime_paging))
        reference=timed_phase('independent_ranking', lambda: retrieve_sales_groups(reference_ranking_sql(plan),read,query_many=many,**paging))
        checks=compare_ranking(ranking,reference)
        if not all(checks.values()):
            print(json.dumps({'case':'T38_core','checks':checks,'progress':PROGRESS}),flush=True)
            return 1
        print(json.dumps({'stage':'ranking_reconciled','scope':'ALL','checks':checks,'progress':PROGRESS}),flush=True)
        checks['every_ranked_sku_detail']=True
        for sku in ranking.ranking:
            detail=timed_phase('runtime_selected_detail', lambda: retrieve_ranked_sku(ranking,sku.sku_code,read,query_many=many,**runtime_paging))
            reference=timed_phase('independent_selected_detail', lambda: retrieve_sales_groups(reference_sql(plan,sku.sku_code),read,query_many=many,**paging))
            current=compare(detail,reference)
            checks['every_ranked_sku_detail'] &= all(current.values())
            if not all(current.values()):
                print(json.dumps({'stage':'detail_comparison_failed','checks':current,'progress':PROGRESS}),flush=True)
                return 1
            PROGRESS['completed_skus']+=1
            print(json.dumps({'stage':'detail_reconciled','checks':current,'progress':PROGRESS}),flush=True)
        PROGRESS['phase']='complete'
    print(json.dumps({'case':'T38_core','checks':checks,'progress':PROGRESS}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case':'T38_core','passed':False,'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None),'progress':PROGRESS}),flush=True)
        raise SystemExit(1)
