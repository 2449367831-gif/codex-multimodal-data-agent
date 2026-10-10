"""DMS points-redemption order summary by province and customer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import sqlglot

from .handlers.base import MetricHandler
from .semantic_types import SemanticAnswer, SemanticRequest, SemanticRuntimeError, _display_table, _format_number, _parse_decimal
from ..sql import SqlSafetyValidator


@dataclass(frozen=True, slots=True)
class DmsRedemptionSummaryPlan:
    start: date
    end: date

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError("DMS redemption interval must be nonempty")


def dms_redemption_summary_request(question: str, *, planner) -> SemanticRequest | None:
    compact = "".join(question.split()).casefold()
    if not all(token in compact for token in ("dms", "积分兑换", "按省区", "客户数", "兑换数量", "空气炸锅")):
        return None
    if any(token in compact for token in ("订单状态", "付款状态", "出库状态")) and "不限制" not in compact:
        return None
    interval = planner._source_interval(question)
    if interval is None:
        return None
    return SemanticRequest(
        route="dms_redemption_by_province", start=interval[0], end=interval[1],
        dms_redemption_summary_plan=DmsRedemptionSummaryPlan(interval[0], interval[1]),
    )


def compile_dms_redemption_summary(plan: DmsRedemptionSummaryPlan) -> str:
    sql = f"""WITH header_ranked AS (
 SELECT TRIM(o.sales_order_code) AS sales_order_code,TRIM(COALESCE(o.province_department_name,'')) AS province,
        NULLIF(TRIM(o.customer_code),'') AS customer_code,o.created_time,
        ROW_NUMBER() OVER(PARTITION BY TRIM(o.sales_order_code) ORDER BY o.updated_time DESC,o.version DESC,o.id DESC) AS rn
 FROM dms_ods.t_sales_order o
 WHERE TRIM(o.sales_order_code)<>'' AND o.created_time>='{plan.start} 00:00:00' AND o.created_time<'{plan.end} 00:00:00'
   AND o.is_points_order=1 AND COALESCE(o.deleted_flag,0)=0 AND LOWER(IFNULL(o._fdl_marked_deleted,'false')) NOT IN ('true','1')
), orders AS (
 SELECT sales_order_code,province,customer_code,created_time FROM header_ranked WHERE rn=1
), details AS (
 SELECT TRIM(d.sales_order_code) AS sales_order_code,TRIM(d.sku_code) AS sku_code,COALESCE(d.bag_quantity,0) AS quantity
 FROM dms_ods.t_sales_order_detail d
 WHERE COALESCE(d.deleted_flag,0)=0 AND LOWER(IFNULL(d._fdl_marked_deleted,'false')) NOT IN ('true','1')
), facts AS (
 SELECT o.province,o.sales_order_code,o.customer_code,d.sku_code,d.quantity
 FROM orders o JOIN details d ON d.sales_order_code=o.sales_order_code
), grouped AS (
 SELECT province,COUNT(DISTINCT sales_order_code) AS order_count,COUNT(DISTINCT customer_code) AS customer_count,
        SUM(quantity) AS redemption_quantity,SUM(CASE WHEN sku_code='8030101' THEN quantity ELSE 0 END) AS air_fryer_quantity
 FROM facts GROUP BY province
), controls AS (
 SELECT COUNT(DISTINCT sales_order_code) AS total_order_count,COUNT(DISTINCT customer_code) AS total_customer_count,
        COALESCE(SUM(quantity),0) AS total_quantity,COALESCE(SUM(CASE WHEN sku_code='8030101' THEN quantity ELSE 0 END),0) AS total_air_fryer_quantity,
        SUM(CASE WHEN quantity<0 THEN 1 ELSE 0 END) AS negative_count,COUNT(DISTINCT province) AS province_count
 FROM facts
)
SELECT g.province,g.order_count,g.customer_count,g.redemption_quantity,g.air_fryer_quantity,
       c.total_order_count,c.total_customer_count,c.total_quantity,c.total_air_fryer_quantity,c.negative_count,c.province_count
FROM grouped g CROSS JOIN controls c ORDER BY g.province"""
    rendered = sqlglot.parse_one(sql, read="doris").sql(dialect="doris")
    SqlSafetyValidator().validate(rendered, allowed_sources=frozenset({"dms_ods.t_sales_order","dms_ods.t_sales_order_detail"}))
    return rendered


class DmsRedemptionSummaryHandler(MetricHandler):
    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.dms_redemption_summary_plan
        rows = self._execute(compile_dms_redemption_summary(plan), purpose="DMS points redemption by province")
        columns=[{"key":"province","kind":"text"},{"key":"order_count","kind":"number","unit":"单"},{"key":"customer_count","kind":"number","unit":"个"},{"key":"redemption_quantity","kind":"number","unit":"件"},{"key":"air_fryer_quantity","kind":"number","unit":"件"}]
        if not rows:
            table=_display_table(table_id="dms_redemption_by_province",title="DMS 积分兑换订单",caption=f"{plan.start} 至 {plan.end} · 按订单创建时间",columns=columns,rows=[],empty_label="当前范围没有有效的积分兑换订单")
            table["complete_reconciled"]=True; table["reviewed_business_table"]=True
            return SemanticAnswer("查询成功，当前范围没有有效的积分兑换订单。",table,result_state="no_data")
        try:
            first=rows[0]; province_count=int(_parse_decimal(first["province_count"])); total_orders=int(_parse_decimal(first["total_order_count"])); total_customers=int(_parse_decimal(first["total_customer_count"])); total_quantity=_parse_decimal(first["total_quantity"]); total_air=_parse_decimal(first["total_air_fryer_quantity"])
            if province_count!=len(rows) or int(_parse_decimal(first["negative_count"]))!=0: raise ValueError
            output=[]; order_sum=customer_sum=0; quantity_sum=air_sum=Decimal(0)
            for row in rows:
                province=str(row.get("province") or "未维护"); orders=int(_parse_decimal(row["order_count"])); customers=int(_parse_decimal(row["customer_count"])); quantity=_parse_decimal(row["redemption_quantity"]); air=_parse_decimal(row["air_fryer_quantity"])
                if orders<0 or customers<0 or quantity<0 or air<0 or air>quantity: raise ValueError
                output.append({"province":province,"order_count":str(orders),"customer_count":str(customers),"redemption_quantity":_format_number(quantity),"air_fryer_quantity":_format_number(air)})
                order_sum+=orders; customer_sum+=customers; quantity_sum+=quantity; air_sum+=air
            if order_sum!=total_orders or quantity_sum!=total_quantity or air_sum!=total_air or customer_sum<total_customers: raise ValueError
        except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
            raise SemanticRuntimeError("DMS 积分兑换汇总未通过对账，暂未展示",reason="dms_summary_quality_failed") from exc
        table=_display_table(table_id="dms_redemption_by_province",title="DMS 积分兑换订单",caption=f"{plan.start} 至 {plan.end} · 按订单创建时间 · 不限制订单状态 · 仅排除逻辑删除",columns=columns,rows=output,summary=[{"label":"兑换订单数","value":str(total_orders),"unit":"单"},{"label":"兑换总量","value":_format_number(total_quantity),"unit":"件"},{"label":"空气炸锅兑换量","value":_format_number(total_air),"unit":"件"}])
        table["complete_reconciled"]=True; table["reviewed_business_table"]=True
        return SemanticAnswer(f"查询成功：积分兑换 {total_orders} 单，兑换数量 {_format_number(total_quantity)} 件，其中空气炸锅 {_format_number(total_air)} 件；省区合计已对账。",table)
