"""T08 independent reference; numeric business results remain in memory only."""

from datetime import datetime,timedelta
from decimal import Decimal,ROUND_HALF_UP,localcontext
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.sales_pages import retrieve_sales_groups
from app.agent_core.box_planning import outbound_box_request


def reference_sql(start,end, *, channel=None, all_actual=False, finished_goods=True):
    if channel not in {None, "线上", "线下", "其他"}:
        raise ValueError("invalid channel")
    type_filter = "(TRIM(h.ref_order_type) IN ('JYCK','HHCK','BFCK','QTCK') OR TRIM(h.ref_order_type) LIKE 'SO%')" if all_actual else "TRIM(h.ref_order_type)='JYCK'"
    product_filter = "EXISTS (SELECT 1 FROM ywzt_ods.master_sku m WHERE TRIM(m.sku_code)=TRIM(d.sku_code) AND TRIM(m.sku_group)='50')" if finished_goods else "TRUE"
    channel_filter = "TRUE" if channel is None else f"channel='{channel}'"
    return f"""WITH distinct_headers AS (
 SELECT DISTINCT TRIM(h.company_code) owner, TRIM(h.outbound_code) doc,
  TRIM(h.facility_code) warehouse, DATE(h.ship_at) day, TRIM(h.store_code) customer
 FROM ywzt_ods.scm_sales_shipment_header h
 WHERE h.ship_at >= '{start} 00:00:00' AND h.ship_at < '{end} 00:00:00'
 AND h.status IN (700,900,701,901) AND {type_filter}
 AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
 AND EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header o
 WHERE TRIM(o.company_code)=TRIM(h.company_code) AND TRIM(o.sales_order_code)=TRIM(h.ref_order_code)
 AND TRIM(o.sales_order_code)<>'' AND COALESCE(o.deleted_flag,0)=0 AND IFNULL(o._fdl_marked_deleted,'false')='false')
), customer_types AS (
 SELECT TRIM(company_code) owner,TRIM(code) customer,MIN(NULLIF(TRIM(type),'')) kind
 FROM ywzt_ods.master_store WHERE TRIM(code)<>'' AND TRIM(company_code)<>'' GROUP BY TRIM(company_code),TRIM(code)
), raw_facts AS (
 SELECT h.owner,h.doc,h.warehouse,h.day,TRIM(d.sku_code) sku,
 CASE WHEN c.kind='Z004' THEN '线上' WHEN c.kind='Z001' THEN '线下' ELSE '其他' END channel,d.ship_qty quantity
 FROM distinct_headers h JOIN ywzt_ods.scm_sales_shipment_detail d
 ON TRIM(d.company_code)=h.owner AND TRIM(d.outbound_code)=h.doc
 LEFT JOIN customer_types c ON c.owner=h.owner AND c.customer=h.customer
 WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
 AND {product_filter}
), facts AS (
 SELECT * FROM raw_facts WHERE {channel_filter}
), units AS (
 SELECT TRIM(sku_code) sku,COUNT(*) records,
 COUNT(DISTINCT CASE WHEN ea_convert_quantity>0 THEN ea_convert_quantity END) variants,
 SUM(CASE WHEN ea_convert_quantity IS NULL OR ea_convert_quantity<=0 THEN 1 ELSE 0 END) invalid,
 MAX(CASE WHEN ea_convert_quantity>0 THEN ea_convert_quantity END) spec
 FROM ywzt_ods.master_sku_unit WHERE TRIM(unit)='40' GROUP BY TRIM(sku_code)
), grouped AS (
 SELECT day,owner,warehouse,sku,channel,SUM(quantity) quantity,COUNT(DISTINCT doc) docs
 FROM facts GROUP BY day,owner,warehouse,sku,channel
)
SELECT g.*,u.spec,u.variants,u.invalid,u.records,(SELECT COUNT(*) FROM grouped) group_count,
 (SELECT SUM(quantity) FROM facts) total_quantity,
 (SELECT COUNT(*) FROM (SELECT DISTINCT owner,doc FROM facts) d) total_documents
FROM grouped g LEFT JOIN units u ON u.sku=g.sku
ORDER BY g.day,g.owner,g.warehouse,g.sku,g.channel LIMIT {100001 if channel is not None else 501}"""


def independently_compare(table, reference):
    expected = {}
    with localcontext() as context:
        context.prec = 100
        for row in reference:
            state = "缺少箱规" if row["records"] is None else "箱规冲突" if row["variants"] > 1 else "箱规为空或非正数" if row["invalid"] > 0 else "已换算"
            spec = Decimal(str(row["spec"])) if state == "已换算" else None
            box = (Decimal(str(row["quantity"]))/spec).quantize(Decimal(1),rounding=ROUND_HALF_UP) if spec is not None else None
            key = tuple(str(row[k]) for k in ("day","owner","warehouse","sku","channel"))
            expected[key] = (Decimal(str(row["quantity"])),int(row["docs"]),spec,box,state)
    actual = {}
    for row in table["rows"]:
        key = tuple(str(row[k]) for k in ("ship_date","cargo_owner","warehouse_code","sku_code","customer_channel"))
        actual[key] = (Decimal(row["quantity"]),int(row["outbound_count"]),Decimal(row["box_spec"]) if row["box_spec"] is not None else None,Decimal(row["box_quantity"]) if row["box_quantity"] is not None else None,row["box_state"])
    summaries = {item["label"]:Decimal(item["value"]) for item in table["summary"]}
    return {
        "all_group_keys_and_measures":actual==expected,
        "no_duplicate_groups":len(actual)==len(table["rows"]) and len(expected)==len(reference),
        "raw_quantity_control":summaries["实际销售出库"]==(Decimal(str(reference[0]["total_quantity"])) if reference else Decimal(0)),
        "independent_document_control":summaries["出库单数"]==(Decimal(str(reference[0]["total_documents"])) if reference else Decimal(0)),
        "rounding_and_missing_explained":any("四舍五入" in note for note in table["notes"]) and any("空值不是零箱" in note for note in table["notes"]),
    }


def main():
    _hide_windows_subprocesses()
    today=datetime.now(ZoneInfo("Asia/Shanghai")).date()
    class TimedBridge(CodexMcpBridge):
        def query(self, *, sql, purpose):
            started=time.monotonic()
            print(json.dumps({"stage":purpose,"event":"started"}),flush=True)
            try:
                return super().query(sql=sql,purpose=purpose)
            finally:
                print(json.dumps({"stage":purpose,"elapsed_seconds":round(time.monotonic()-started,2)}),flush=True)
    bridge=TimedBridge(project_root=ROOT,backend_url="http://127.0.0.1:8010",timeout_seconds=180)
    runtime=SemanticQueryRuntime(query_client=bridge)
    cases=json.loads((ROOT/"tests/acceptance/complex-query-cases.json").read_text(encoding="utf-8"))["cases"]
    question=next(case["question"] for case in cases if case["id"]=="T08")
    plan=outbound_box_request(question,interval=(today-timedelta(days=1),today),resolver=runtime.scope_resolver,contract_root=ROOT)
    result=runtime.execute_plan(plan,question=question)
    print(json.dumps({"case":"T08","stage":"runtime_complete","live":result["result_state"] in {"live_success","no_data"}}),flush=True)
    def query(sql,*,purpose):
        return validate_doris_result(bridge.query(sql=sql,purpose="T08 independent reference")).rows
    reference=retrieve_sales_groups(reference_sql(today-timedelta(days=1),today),query)
    checks=independently_compare(result["business_table"],reference)
    print(json.dumps({"case":"T08","checks":checks}))
    return 0 if all(checks.values()) else 1


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case":"T08","error_type":type(exc).__name__,"reason":getattr(exc,"reason",None)}))
        raise SystemExit(2)
