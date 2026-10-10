"""Independently reconcile T07 without recording business quantities or rows."""

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.runtime import SemanticQueryRuntime
from app.chat_api import _public_business_table
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


def independent_sql(start, end):
    # This branch aggregates physical detail by document and SKU, independent
    # of the production compiler's separate quantity/document aggregation.
    return f"""WITH eligible AS (
  SELECT DISTINCT TRIM(h.company_code) AS owner, TRIM(h.outbound_code) AS outbound_code
  FROM ywzt_ods.scm_sales_shipment_header h
  WHERE h.ship_at >= '{start.isoformat()} 00:00:00' AND h.ship_at < '{end.isoformat()} 00:00:00'
    AND h.status IN (700,900,701,901)
    AND (TRIM(h.ref_order_type) IN ('JYCK','HHCK','BFCK','QTCK') OR TRIM(h.ref_order_type) LIKE 'SO%')
    AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
    AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header o
      WHERE TRIM(o.company_code)=TRIM(h.company_code) AND TRIM(o.sales_order_code)=TRIM(h.ref_order_code)
        AND TRIM(o.sales_order_code)<>'' AND COALESCE(o.deleted_flag,0)=0 AND IFNULL(o._fdl_marked_deleted,'false')='false')
), documents AS (
  SELECT TRIM(d.company_code) AS owner, TRIM(d.outbound_code) AS outbound_code,
         TRIM(d.sku_code) AS sku_code, SUM(d.ship_qty) AS quantity
  FROM ywzt_ods.scm_sales_shipment_detail d
  WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
    AND EXISTS (SELECT 1 FROM eligible h WHERE h.owner=TRIM(d.company_code) AND h.outbound_code=TRIM(d.outbound_code))
    AND (TRIM(d.sku_code) IN ('5101010010008','5101010010009')
      OR EXISTS (SELECT 1 FROM ywzt_ods.master_sku s WHERE TRIM(s.sku_code)=TRIM(d.sku_code) AND TRIM(s.sku_group)='50'))
  GROUP BY TRIM(d.company_code), TRIM(d.outbound_code), TRIM(d.sku_code)
), all_documents AS (
  SELECT DISTINCT owner,outbound_code FROM documents
)
SELECT sku_code,SUM(quantity) AS quantity,COUNT(*) AS outbound_count,
       (SELECT COUNT(*) FROM all_documents) AS company_outbound_count
FROM documents GROUP BY sku_code ORDER BY sku_code LIMIT 501"""


def compare(table, reference):
    actual = {row['sku_code']: (Decimal(row['quantity']), int(row['outbound_count'])) for row in table['rows']}
    control = {row['sku_code']: (Decimal(str(row['quantity'])), int(row['outbound_count'])) for row in reference}
    return {
        'all_sku_groups_match': actual == control,
        'reference_complete': len(reference) <= 500,
        'unique_sku_rows': len(actual) == len(table['rows']),
        'quantity_total_matches': Decimal(table['summary'][0]['value']) == sum((value[0] for value in control.values()), Decimal(0)),
        'company_documents_match': int(table['summary'][1]['value']) == (int(reference[0]['company_outbound_count']) if reference else 0),
        'exceptions_explained': all(code in ''.join(table['notes']) for code in ('5101010010008','5101010010009')),
    }


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    runtime = SemanticQueryRuntime(query_client=bridge, clock=lambda: datetime.now(ZoneInfo('Asia/Shanghai')))
    cases = json.loads((ROOT / 'tests/acceptance/complex-query-cases.json').read_text(encoding='utf-8'))['cases']
    question = next(case['question'] for case in cases if case['id'] == 'T07')
    request = runtime.classify(question)
    assert request and request.route == 'sales_aggregate' and request.scope.owner == 'ALL'
    result = runtime.execute_plan(request, question=question)
    table = _public_business_table(result['business_table'], live=True)
    assert table is not None
    reference = validate_doris_result(bridge.query(sql=independent_sql(request.start,request.end), purpose='T07 independent document and SKU reconciliation')).rows
    checks = compare(table, reference)
    checks['live_evidence'] = result['query_execution']['live_business_result_executed']
    print(json.dumps({'case':'T07','checks':checks}))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case':'T07','state':'verification_failed','error_type':type(exc).__name__,'reason':getattr(exc,'reason','verification_failed')}))
        raise SystemExit(2)
