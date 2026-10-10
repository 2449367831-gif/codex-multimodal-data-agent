"""Independent T09 name/quantity reconciliation; persist booleans only."""

from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.query_scope import QueryScope
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRequest
from app.agent_core.sales_aggregation import SalesAggregation


def reference_sql():
    return """WITH heads AS (
 SELECT DISTINCT TRIM(h.company_code) owner,TRIM(h.outbound_code) doc,TRIM(h.ref_order_code) source_order
 FROM ywzt_ods.scm_sales_shipment_header h
 WHERE h.status IN (700,900,701,901)
 AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%')
 AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
 AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header o WHERE TRIM(o.company_code)=TRIM(h.company_code)
 AND TRIM(o.sales_order_code)=TRIM(h.ref_order_code) AND TRIM(o.sales_order_code)<>''
 AND COALESCE(o.deleted_flag,0)=0 AND IFNULL(o._fdl_marked_deleted,'false')='false')
), shipped AS (
 SELECT h.owner,h.doc,h.source_order,TRIM(d.sku_code) sku,d.ship_qty qty,NULLIF(TRIM(d.sku_name),'') label
 FROM heads h JOIN ywzt_ods.scm_sales_shipment_detail d ON TRIM(d.company_code)=h.owner AND TRIM(d.outbound_code)=h.doc
 WHERE TRIM(d.sku_code)='QY-YWKC500G' AND COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
), sku_keys AS (
 SELECT DISTINCT owner,sku FROM shipped
), measures AS (
 SELECT owner,sku,SUM(qty) quantity,COUNT(DISTINCT doc) docs FROM shipped GROUP BY owner,sku
), observations AS (
 SELECT owner,sku,'shipment' origin,label FROM shipped
 UNION ALL
 SELECT k.owner,k.sku,'master',NULLIF(TRIM(m.sku_name),'') FROM sku_keys k
 JOIN ywzt_ods.master_sku m ON TRIM(m.sku_code)=k.sku
 UNION ALL
 SELECT TRIM(d.company_code),TRIM(d.sku_code),'order',NULLIF(TRIM(d.sku_name),'')
 FROM ywzt_ods.scm_sales_order_detail d WHERE IFNULL(d._fdl_marked_deleted,'false')='false'
 AND EXISTS (SELECT 1 FROM shipped s WHERE s.owner=TRIM(d.company_code) AND s.source_order=TRIM(d.sales_order_code) AND s.sku=TRIM(d.sku_code))
), grouped AS (
 SELECT owner,sku,origin,label,COUNT(*) records FROM observations GROUP BY owner,sku,origin,label
)
SELECT g.*,m.quantity,m.docs,(SELECT COUNT(*) FROM grouped) observation_groups,
 (SELECT SUM(qty) FROM shipped) total_quantity,
 (SELECT COUNT(*) FROM (SELECT DISTINCT owner,doc FROM shipped) x) total_documents
FROM grouped g JOIN measures m ON m.owner=g.owner AND m.sku=g.sku
ORDER BY g.owner,g.sku,g.origin,g.label LIMIT 501"""


def compare(table,observations):
    grouped=defaultdict(lambda:defaultdict(dict))
    measures={}
    for row in observations:
        if row['observation_groups'] != len(observations):
            raise ValueError('incomplete reference')
        key=(row['owner'],row['sku'])
        names=grouped[key][row['origin']]
        if row['label'] in names:
            raise ValueError('duplicate reference observation')
        names[row['label']]=int(row['records'])
        measure=(Decimal(str(row['quantity'])),int(row['docs']))
        if key in measures and measures[key]!=measure:
            raise ValueError('reference changed')
        measures[key]=measure
    expected={}
    for key,sources in grouped.items():
        name_sets={source:{name for name in sources[source] if name is not None} for source in ('master','shipment','order')}
        def unique(source):
            return next(iter(name_sets[source])) if len(name_sets[source])==1 else None
        variants=len(set().union(*name_sets.values()))
        missing=sum(sources[source].get(None,0) for source in name_sets)
        missing_sources=sum(not sources[source] for source in name_sets)
        status='；'.join([('主数据名称' if unique('master') else '显示编码'),*(['名称不一致'] if variants>1 else []),*(['存在缺失'] if missing or missing_sources else [])])
        expected[key]=(unique('master') or key[1],*measures[key],unique('shipment'),unique('order'),len(name_sets['master']),missing,missing_sources,variants,status)
    actual={}
    for row in table['rows']:
        key=(row['cargo_owner'],row['sku_code'])
        actual[key]=(row['sku_name'],Decimal(str(row['quantity'])),int(row['outbound_count']),row['shipment_name'],row['order_name'],
            *[int(row[k]) for k in ('master_name_variants','missing_name_rows','missing_name_sources','name_variants')],row['name_status'])
    summaries={s['label']:Decimal(s['value']) for s in table['summary']}
    return {'all_keys_names_counts_and_quantities':actual==expected,
        'no_duplicate_groups':len(actual)==len(table['rows']),
        'company_quantity':summaries.get('实际销售出库',Decimal(0))==(Decimal(str(observations[0]['total_quantity'])) if observations else Decimal(0)),
        'independent_documents':summaries.get('出库单数',Decimal(0))==(Decimal(str(observations[0]['total_documents'])) if observations else Decimal(0)),
        'all_time':table['caption'].startswith('全部时间'),'name_policy_explained':len(table['notes'])==3}


def main():
    _hide_windows_subprocesses()
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    request=SemanticRequest('sales_sku_name_quality',None,None,scope=QueryScope(sku_codes=('QY-YWKC500G',)),sales_aggregation=SalesAggregation(('cargo_owner','sku')))
    result=SemanticQueryRuntime(query_client=bridge).execute_plan(request,question='SKU名称独立验证')
    print(json.dumps({'stage':'runtime_complete','result_state':result['result_state']}),flush=True)
    reference=validate_doris_result(bridge.query(sql=reference_sql(),purpose='T09 independent name provenance and quantities')).rows
    checks=compare(result['business_table'],reference)
    print(json.dumps({'case':'T09_runtime','checks':checks}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None)}))
        raise SystemExit(2)
