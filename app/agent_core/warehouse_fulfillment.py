"""Daily finished-goods outbound split by warehouse and 2C/2B."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re

import sqlglot

from .handlers.base import MetricHandler
from .semantic_types import SemanticAnswer, SemanticRequest, SemanticRuntimeError, _display_table, _format_number, _parse_decimal
from ..query_scope import QueryScope
from ..outbound_contract import completed_status_sql, ref_order_type_sql
from ..sql import SqlSafetyValidator


@dataclass(frozen=True, slots=True)
class WarehouseFulfillmentPlan:
    start: date
    end: date
    owner: str
    warehouse_names: tuple[str, ...]

    def __post_init__(self):
        if self.end <= self.start or self.owner == "ALL" or len(self.warehouse_names) < 2:
            raise ValueError("warehouse fulfillment scope is incomplete")


def _warehouse_names(question: str) -> tuple[str, ...]:
    text = re.sub(r"(?:最近|近|过去)\s*\d+\s*(?:个)?(?:完整)?(?:自然)?(?:天|日)", "", question)
    text = re.sub(r"^(?:查询|查|统计|请)\s*", "", text)
    m = re.search(r"([\u4e00-\u9fff]{2,12}仓(?:(?:、|，|,|和|与|及)[\u4e00-\u9fff]{2,12}仓)+)(?:的)?(?:产成品|成品)?出库", text)
    if not m:
        return ()
    return tuple(dict.fromkeys(x.strip() for x in re.split(r"(?:和|与|及|、)", m.group(1)) if x.strip()))


def warehouse_fulfillment_request(question: str, *, planner) -> SemanticRequest | None:
    compact = "".join(question.split()).casefold()
    if not (("产成品出库" in compact or "成品出库" in compact) and "2c" in compact and "2b" in compact):
        return None
    if "按日期" not in compact or not any(x in compact for x in ("按仓库", "日期和仓库", "仓库汇总")) or not any(x in compact for x in ("总实发数量", "总实发")):
        return None
    names = _warehouse_names(question)
    interval = planner._source_interval(question)
    if len(names) < 2 or interval is None:
        return None
    return SemanticRequest(
        route="sales_outbound_by_warehouse_fulfillment", start=interval[0], end=interval[1],
        scope=QueryScope(owner="HJXH", warehouse_labels=names, finished_goods=True),
        finished_goods_only=True,
        warehouse_fulfillment_plan=WarehouseFulfillmentPlan(interval[0], interval[1], "HJXH", names),
    )


def compile_warehouse_fulfillment(plan: WarehouseFulfillmentPlan) -> str:
    owner = plan.owner.replace("'", "''")
    def match_term(value: str) -> str:
        if "弘一" in value:
            return "弘一"
        if "鲜生活" in value:
            return "鲜生活"
        return value
    names = " OR ".join("LOCATE('" + match_term(x).replace("'", "''") + "', warehouse_name) > 0" for x in plan.warehouse_names)
    sql = f"""WITH wms AS (
 SELECT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,TRIM(wms_code) AS wms_code,
        TRIM(COALESCE(wms_desc,'')) AS warehouse_name,
        ROW_NUMBER() OVER(PARTITION BY TRIM(CAST(cargo_owner_id AS STRING)),TRIM(wms_code) ORDER BY updated_date DESC,version DESC,id DESC) AS rn
 FROM ywzt_ods.master_wms WHERE TRIM(CAST(cargo_owner_id AS STRING))='{owner}' AND COALESCE(TRIM(CAST(status AS STRING)),'0')='0'
), target_wms AS (
 SELECT cargo_owner,wms_code,warehouse_name FROM wms WHERE rn=1 AND ({names})
), finished_skus AS (
 SELECT DISTINCT TRIM(sku_code) AS sku_code FROM ywzt_ods.master_sku WHERE TRIM(CAST(sku_group AS STRING))='50' AND TRIM(sku_code)<>''
), headers AS (
 SELECT DISTINCT TRIM(h.company_code) AS cargo_owner,TRIM(h.outbound_code) AS outbound_code,TRIM(h.facility_code) AS wms_code,
   DATE(h.ship_at) AS ship_date,CASE WHEN COALESCE(TRIM(CAST(h.terms_of_shipment AS STRING)),'')='20' THEN '2C' ELSE '2B' END AS fulfillment_type
 FROM ywzt_ods.scm_sales_shipment_header h JOIN target_wms w ON w.cargo_owner=TRIM(h.company_code) AND w.wms_code=TRIM(h.facility_code)
 WHERE TRIM(h.company_code)='{owner}' AND h.ship_at>='{plan.start} 00:00:00' AND h.ship_at<'{plan.end} 00:00:00'
   AND {completed_status_sql('h')} AND {ref_order_type_sql('h')} AND COALESCE(h.delete_flag,0)=0 AND IFNULL(h._fdl_marked_deleted,'false')='false'
), facts AS (
 SELECT h.ship_date,h.wms_code,h.fulfillment_type,TRIM(d.sku_code) AS sku_code,COALESCE(d.ship_qty,0) AS quantity,h.outbound_code
 FROM headers h JOIN ywzt_ods.scm_sales_shipment_detail d ON TRIM(d.company_code)=h.cargo_owner AND TRIM(d.outbound_code)=h.outbound_code
 JOIN finished_skus f ON f.sku_code=TRIM(d.sku_code)
 WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
), grouped AS (
 SELECT ship_date,wms_code,fulfillment_type,SUM(quantity) AS quantity,COUNT(DISTINCT outbound_code) AS outbound_count FROM facts GROUP BY ship_date,wms_code,fulfillment_type
), controls AS (
 SELECT COALESCE(SUM(quantity),0) AS total_quantity,SUM(CASE WHEN quantity<0 THEN 1 ELSE 0 END) AS negative_count,COUNT(*) AS source_rows FROM facts
)
SELECT g.ship_date,g.wms_code,w.warehouse_name,g.fulfillment_type,g.quantity,g.outbound_count,c.total_quantity,c.negative_count,c.source_rows,COUNT(*) OVER() AS group_count
FROM grouped g JOIN target_wms w ON w.cargo_owner='{owner}' AND w.wms_code=g.wms_code CROSS JOIN controls c
ORDER BY g.ship_date,g.wms_code,g.fulfillment_type"""
    rendered = sqlglot.parse_one(sql, read="doris").sql(dialect="doris")
    SqlSafetyValidator().validate(rendered, allowed_sources=frozenset({"ywzt_ods.master_wms","ywzt_ods.master_sku","ywzt_ods.scm_sales_shipment_header","ywzt_ods.scm_sales_shipment_detail"}))
    return rendered


class WarehouseFulfillmentHandler(MetricHandler):
    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.warehouse_fulfillment_plan
        rows = self._execute(compile_warehouse_fulfillment(plan), purpose="finished-goods outbound by warehouse and fulfillment")
        columns=[{"key":"ship_date","kind":"text"},{"key":"warehouse_name","kind":"text"},{"key":"fulfillment_2c","kind":"number","unit":"EA"},{"key":"fulfillment_2b","kind":"number","unit":"EA"},{"key":"total_quantity","kind":"number","unit":"EA"}]
        if not rows:
            table=_display_table(table_id="sales_outbound_by_warehouse_fulfillment",title="成品销售出库 2C/2B",caption=f"{plan.start} 至 {plan.end} · 左闭右开",columns=columns,rows=[],empty_label="当前范围没有有效产成品出库记录")
            table["complete_reconciled"]=True; table["reviewed_business_table"]=True
            return SemanticAnswer("查询成功，当前范围没有有效产成品出库记录。",table,result_state="no_data")
        try:
            total=_parse_decimal(rows[0]["total_quantity"]); group_count=int(_parse_decimal(rows[0]["group_count"]))
            if group_count!=len(rows) or any(int(_parse_decimal(r["negative_count"])) for r in rows): raise ValueError
            groups={}
            for r in rows:
                key=(str(r["ship_date"]),str(r["warehouse_name"]))
                groups.setdefault(key,[Decimal(0),Decimal(0)])[0 if r["fulfillment_type"]=="2C" else 1]+=_parse_decimal(r["quantity"])
            output=[]; total_sum=Decimal(0)
            for (day,name), values in sorted(groups.items()):
                item_total=values[0]+values[1]; total_sum+=item_total
                output.append({"ship_date":day,"warehouse_name":name,"fulfillment_2c":_format_number(values[0]),"fulfillment_2b":_format_number(values[1]),"total_quantity":_format_number(item_total)})
            if total_sum!=total: raise ValueError
        except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
            raise SemanticRuntimeError("2C/2B 出库数量未通过对账，暂未展示",reason="sales_aggregate_reconciliation_failed") from exc
        table=_display_table(table_id="sales_outbound_by_warehouse_fulfillment",title="成品销售出库 2C/2B",caption=f"{plan.start} 至 {plan.end} · 左闭右开 · 不含今天",columns=columns,rows=output,summary=[{"label":"实际销售出库","value":_format_number(total),"unit":"EA"}])
        table["complete_reconciled"]=True; table["reviewed_business_table"]=True
        return SemanticAnswer(f"查询成功：成品实际销售出库合计 {_format_number(total)} EA，2C、2B与总量已完成对账。",table)
