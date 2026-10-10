"""Independent T25 order-header reference; prints checks, never source rows."""

from collections import Counter
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.agent_core.dms_points_orders import DmsPointsOrderPlan, retrieve_dms_points_orders
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


def reference_sql():
    return """WITH ordered_headers AS (
 SELECT o.*,ROW_NUMBER() OVER (PARTITION BY TRIM(o.sales_order_code)
   ORDER BY o.updated_time DESC NULLS LAST,o.version DESC NULLS LAST,o.id DESC) AS rn
 FROM dms_ods.t_sales_order o WHERE NULLIF(TRIM(o.sales_order_code),'') IS NOT NULL
), current_points AS (
 SELECT TRIM(sales_order_code) AS sales_order_code,NULLIF(TRIM(customer_code),'') AS customer_code,
   NULLIF(TRIM(customer_name),'') AS customer_name,created_time
 FROM ordered_headers WHERE rn=1 AND is_points_order=1
   AND COALESCE(deleted_flag,0)=0 AND LOWER(TRIM(COALESCE(_fdl_marked_deleted,''))) IN ('','false','0')
   AND created_time >= '2026-01-01 00:00:00' AND created_time < '2026-09-01 00:00:00'
)
SELECT *,COUNT(*) OVER () AS group_count FROM current_points ORDER BY sales_order_code LIMIT 501"""


def compare(result, reference):
    def timestamp(value):
        parsed = datetime.fromisoformat(str(value))
        return (parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if parsed.tzinfo is None
                else parsed.astimezone(ZoneInfo('Asia/Shanghai')))
    expected = {r['sales_order_code']:(r['customer_code'],r['customer_name'],timestamp(r['created_time'])) for r in reference}
    actual = {r['sales_order_code']:(r['customer_code'],r['customer_name'],timestamp(r['created_time'])) for r in result.orders}
    months = Counter(timestamp(row['created_time']).strftime('%Y-%m') for row in reference)
    return {'orders_unique': len(expected)==len(reference), 'order_details': actual==expected,
        'order_keys': actual.keys()==expected.keys(),
        'customer_codes': all(k in expected and v[0]==expected[k][0] for k,v in actual.items()),
        'customer_names': all(k in expected and v[1]==expected[k][1] for k,v in actual.items()),
        'created_times': all(k in expected and v[2]==expected[k][2] for k,v in actual.items()),
        'monthly_counts': {row['month']:row['order_count'] for row in result.months}==dict(months),
        'total_count': result.order_count==len(reference),
        'missing_customers': result.missing_customer_code_count==sum(r['customer_code'] is None for r in reference)
            and result.missing_customer_name_count==sum(r['customer_name'] is None for r in reference)}


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    def read(sql, *, purpose):
        return validate_doris_result(bridge.query(sql=sql,purpose=purpose)).rows
    result = retrieve_dms_points_orders(DmsPointsOrderPlan(start=date(2026,1,1),end=date(2026,9,1)),read)
    reference = retrieve_sales_groups(reference_sql(),read)
    checks = compare(result,reference)
    print(json.dumps({'case':'T25','checks':checks}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None)}),flush=True)
        raise SystemExit(2)
