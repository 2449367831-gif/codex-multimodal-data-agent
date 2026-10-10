"""Request-scoped customer price-group breakdown, distinct from platform channels."""

from fractions import Fraction
import re

import sqlglot

from .sales_aggregation import SalesAggregation, compile_sales_aggregation
from .semantic_types import SemanticRequest, SemanticAnswer, SemanticRuntimeError, _display_table, _parse_decimal, _parse_source_datetime
from .handlers.base import MetricHandler
from ..sku_references import extract_sku_references, restore_sku_references


ENUM_PARENT = "2c9c0da1948322ec01948bac2924000a"
_PARENT_ID = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,118}[A-Za-z0-9_])?"
_MAPPING_CLAUSE = re.compile(
    r"(?:渠道细分(?:取|按)|渠道取)master_store\.price_group[，,；;]关联枚举父级[:：]?"
    r"(?P<parent>" + _PARENT_ID + r")(?=$|[，,；;。]|-(?=[\u4e00-\u9fff]))", re.I
)


def price_group_request(question, *, planner):
    sku_codes, semantic_question = extract_sku_references(question)
    original = re.sub(r"\s+", "", semantic_question).replace("`", "")
    # Only an explicit current-request mapping selects this variant. A bare
    # "channel" must not silently become price-group or platform classification.
    mappings = list(_MAPPING_CLAUSE.finditer(original))
    parents = {match['parent'] for match in mappings}
    customer_master_default = bool(re.search(
        r"渠道细分.*(?:客户主数据|price_group)|(?:客户主数据|price_group).*渠道细分",
        original,
        re.I,
    ))
    if len(parents) > 1:
        return None
    if parents:
        parent = parents.pop()
    else:
        # The business phrase itself is unambiguous: "渠道细分 + 2C/2B"
        # means the reviewed customer-master price-group breakdown. Users do
        # not need to know the internal enum parent identifier. Keep the
        # explicit mapping form above so a request can still override it.
        if not (customer_master_default or re.search(
            r"渠道细分.*2c\s*/\s*2b|2c\s*/\s*2b.*渠道细分", original, re.I
        )):
            return None
        parent = ENUM_PARENT
    text = _MAPPING_CLAUSE.sub('', original).lower()
    if "销售出库" not in text or not re.search(r"(?:出库单数|实发数量|实发量|数量)", text):
        return None
    # Consume the entire status list, not a matching prefix: an extra status
    # changes the requested population even when its digits look like residue.
    statuses = list(re.finditer(r"出库状态[:：]?(\d+(?:[、,，/]\d+)*)", text))
    if any(set(re.split(r"[、,，/]", match[1])) != {"700", "900", "701", "901"} for match in statuses):
        return None
    text = re.sub(r"出库状态[:：]?(\d+(?:[、,，/]\d+)*)", "", text)
    # A definition in the rules is still a filter, even if the headline did
    # not also say finished goods. Keep it for the scope resolver.
    text = re.sub(
        r"(?:产?成品|产品组)\s*[:：]?\s*(?:master_sku\.sku_group\s*=\s*(?:'50'|\"50\")|50)",
        "成品",
        text,
    )
    text = re.sub(
        r"2c\s*[:：]?\s*terms_of_shipment\s*=\s*(?:'20'|\"20\"|20)",
        "",
        text,
    )
    text = text.replace("排除逻辑删除数据", "").replace("口径：", "").replace("口径:", "")
    # Consume only recognized presentation clauses, never arbitrary trailing
    # predicates: a warehouse, SKU or exclusion must survive scope resolution.
    text = re.sub(
        r"按[“‘\"']?渠道细分\s*(?:\+|和|与|及)\s*2c\s*/\s*2b[”’\"']?汇总",
        "",
        text,
    )
    text = re.sub(r"渠道细分按客户主数据分类", "", text)
    if customer_master_default:
        text = re.sub(r"渠道细分(?:使用|取|按)?客户主数据(?:的)?(?:price_group|分类)?", "", text)
        text = re.sub(r"渠道细分(?:使用|取|按)?price_group", "", text)
        text = re.sub(r"检查(?:各渠道|按渠道)(?:合计|汇总).*", "", text)
        text = re.sub(r"按渠道细分(?:汇总|统计|展示)?", "", text)
        text = re.sub(r"(?:本月|上月|本周|截至(?:昨天|昨日|今天|今日)|昨天|昨日|今天|今日)", "", text)
        text = re.sub(r"销售出库|2c|2b|各渠道|渠道合计|合计是否等于总出库量", "", text)
        text = re.sub(r"[，,。；;、和与及并的：:（）()]+", "", text)
    text = re.sub(r"出库单(?:件|笔|单)?数|实发数量|sku数|全天|输出|查询", "", text)
    # "产品" alone is not a finished-goods filter. An explicit group rule or
    # the term "成品" supplies that predicate independently.
    text = re.sub(r"(?:生产)?产品(?=销售出库)", "", text)
    if re.search(r"[<>=≤≥]", text):
        return None
    text = text.replace("产成品", "成品")
    # ScopeResolver rejects any unconsumed predicate. Never rebuild a scope
    # from just a detected owner and discard SKU, warehouse or exclusions.
    scope, _, unsupported = planner.scope_resolver.resolve(restore_sku_references(text, sku_codes))
    if unsupported:
        return None
    interval = planner._source_interval(semantic_question)
    if interval is None:
        return None
    return SemanticRequest("sales_price_group", interval[0], interval[1], scope=scope,
                           price_group_parent=parent,
                           finished_goods_only=scope.finished_goods,
                           sales_aggregation=SalesAggregation(("customer_channel", "fulfillment_type"),
                               fulfillment_type=planner._requested_fulfillment_type(original.lower()),
                               ref_order_types=planner._requested_order_types(text)))


def compile_price_group_sales(start, end, *, finished_goods_only=True, order_types=(), fulfillment_type=None, enum_parent=ENUM_PARENT):
    if not isinstance(enum_parent, str) or re.fullmatch(_PARENT_ID, enum_parent) is None:
        raise ValueError("invalid channel parent identifier")
    parent_literal = sqlglot.exp.Literal.string(enum_parent).sql(dialect="doris")
    base = sqlglot.parse_one(compile_sales_aggregation(
        SalesAggregation(("customer_channel", "fulfillment_type"), fulfillment_type=fulfillment_type, ref_order_types=order_types), start=start, end=end,
        finished_goods_only=finished_goods_only
    ), read="doris")
    retained = [cte for cte in base.args["with_"].expressions
                if cte.alias_or_name in {"header_variants", "headers", "finished_skus", "detail_by_sku"}]
    result = sqlglot.parse_one(f"""WITH customer_groups AS (
 SELECT TRIM(s.company_code) AS cargo_owner, TRIM(s.code) AS store_code,
   MIN(NULLIF(TRIM(s.price_group), '')) AS price_group,
   COUNT(DISTINCT COALESCE(NULLIF(TRIM(s.price_group), ''), '<missing>')) AS variants,
   MAX(s.updated) AS source_updated
 FROM ywzt_ods.master_store s
 WHERE COALESCE(s.delete_flag, 0) = 0 AND TRIM(s.code) <> '' AND TRIM(s.company_code) <> ''
   AND EXISTS (SELECT 1 FROM headers h WHERE h.cargo_owner = TRIM(s.company_code) AND h.store_code = TRIM(s.code))
 GROUP BY TRIM(s.company_code), TRIM(s.code)
), channel_labels AS (
 SELECT TRIM(e.CODE_) AS code, MIN(NULLIF(TRIM(e.NAME_), '')) AS name,
   COUNT(DISTINCT COALESCE(NULLIF(TRIM(e.NAME_), ''), '<missing>')) AS variants
 FROM ywzt_ods.sys_enum e
  WHERE TRIM(e.PARENT_CODE_) = {parent_literal} AND COALESCE(e.DELETED_, 0) = 0 AND TRIM(e.CODE_) <> ''
 GROUP BY TRIM(e.CODE_)
), facts AS (
 SELECT h.cargo_owner,h.outbound_code,h.ship_at,h.header_variants,h.fulfillment_type,
   d.sku_code,d.quantity,d.null_quantity_count,d.negative_quantity_count,
   COALESCE(c.price_group, '未维护') AS channel_code,
   COALESCE(e.name, '未匹配渠道') AS channel_name,
   COALESCE(c.variants,0) AS customer_variants, COALESCE(e.variants,0) AS enum_variants,
   CASE WHEN c.price_group IS NULL OR e.name IS NULL THEN 1 ELSE 0 END AS unmapped,
   c.source_updated
 FROM headers h JOIN detail_by_sku d ON d.cargo_owner=h.cargo_owner AND d.outbound_code=h.outbound_code
 LEFT JOIN customer_groups c ON c.cargo_owner=h.cargo_owner AND c.store_code=h.store_code
 LEFT JOIN channel_labels e ON e.code=c.price_group
), quantities AS (
 SELECT channel_code,channel_name,fulfillment_type,SUM(quantity) AS quantity,
   COUNT(DISTINCT sku_code) AS sku_count
 FROM facts GROUP BY channel_code,channel_name,fulfillment_type
), document_keys AS (
 SELECT DISTINCT channel_code,channel_name,fulfillment_type,cargo_owner,outbound_code FROM facts
), document_counts AS (
 SELECT channel_code,channel_name,fulfillment_type,COUNT(*) AS outbound_count FROM document_keys
 GROUP BY channel_code,channel_name,fulfillment_type
), all_documents AS (
 SELECT DISTINCT cargo_owner,outbound_code FROM facts
), controls AS (
 SELECT COALESCE(SUM(quantity),0) AS total_quantity,COUNT(DISTINCT sku_code) AS total_sku_count,
   COALESCE(SUM(CASE WHEN header_variants>1 OR customer_variants>1 OR enum_variants>1 THEN 1 ELSE 0 END),0) AS conflict_count,
   COALESCE(SUM(null_quantity_count),0) AS null_quantity_count,
   COALESCE(SUM(negative_quantity_count),0) AS negative_quantity_count,
   COALESCE(SUM(CASE WHEN COALESCE(cargo_owner,'')='' OR COALESCE(outbound_code,'')='' OR COALESCE(sku_code,'')='' THEN 1 ELSE 0 END),0) AS blank_key_count,
   COALESCE(SUM(CASE WHEN unmapped=1 THEN quantity ELSE 0 END),0) AS unmapped_quantity,
   MAX(ship_at) AS latest_shipment_at,MAX(source_updated) AS customer_updated_at
 FROM facts
)
SELECT q.*,n.outbound_count,c.*,(SELECT COUNT(*) FROM all_documents) AS total_outbound_count,
   (SELECT COUNT(*) FROM quantities) AS group_count
FROM quantities q JOIN document_counts n ON n.channel_code=q.channel_code AND n.channel_name=q.channel_name AND n.fulfillment_type=q.fulfillment_type
CROSS JOIN controls c ORDER BY q.channel_code,q.fulfillment_type LIMIT 501""", read="doris")
    result.args["with_"].set("expressions", [*retained, *result.args["with_"].expressions])
    return result.sql(dialect="doris")


def count(value):
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError("invalid count")
    return int(number)


class PriceGroupSalesHandler(MetricHandler):
    def run(self, request):
        rows = self._execute(compile_price_group_sales(request.start, request.end,
                                                       enum_parent=request.price_group_parent or ENUM_PARENT,
                                                       finished_goods_only=request.finished_goods_only,
                                                       order_types=request.sales_aggregation.order_types if request.sales_aggregation else (),
                                                       fulfillment_type=request.sales_aggregation.fulfillment_type if request.sales_aggregation else None),
                             purpose="actual shipment channel and fulfillment reconciliation")
        if len(rows) > 500:
            raise SemanticRuntimeError("分组结果超过展示上限，未展示不完整结果", reason="result_truncated")
        displayed, seen, global_control = [], set(), None
        quantity_sum, document_sum = Fraction(), 0
        try:
            for row in rows:
                controls = {key: count(row[key]) for key in ("total_sku_count", "total_outbound_count", "group_count",
                            "conflict_count", "null_quantity_count", "negative_quantity_count", "blank_key_count")}
                controls.update(total_quantity=_parse_decimal(row["total_quantity"]),
                                unmapped_quantity=_parse_decimal(row["unmapped_quantity"]),
                                latest_shipment_at=row["latest_shipment_at"],customer_updated_at=row["customer_updated_at"])
                if global_control is not None and controls != global_control:
                    raise ValueError("inconsistent controls")
                global_control = controls
                if controls["group_count"] != len(rows) or any(controls[k] for k in (
                    "conflict_count", "null_quantity_count", "negative_quantity_count", "blank_key_count")):
                    raise ValueError("source conflict or incomplete result")
                latest = _parse_source_datetime(controls["latest_shipment_at"], timezone_name=self.timezone_name)
                if not request.start <= latest.date() < request.end:
                    raise ValueError("source time outside request")
                keys = tuple(str(row.get(k) or "").strip() for k in ("channel_code","channel_name","fulfillment_type"))
                if not all(keys) or keys in seen or keys[2] not in {"2C","2B","未配置"}:
                    raise ValueError("invalid grouping")
                seen.add(keys)
                quantity, documents, skus = _parse_decimal(row["quantity"]), count(row["outbound_count"]), count(row["sku_count"])
                if quantity < 0 or not 0 < documents <= controls["total_outbound_count"] or not 0 < skus <= controls["total_sku_count"]:
                    raise ValueError("invalid measures")
                displayed.append(dict(zip(("channel_code","channel_name","fulfillment_type"),keys),
                                      quantity=format(quantity,"f"),outbound_count=str(documents),sku_count=str(skus)))
                quantity_sum += Fraction(quantity)
                document_sum += documents
            if rows and (quantity_sum != global_control["total_quantity"] or document_sum != global_control["total_outbound_count"]
                         or not 0 <= global_control["unmapped_quantity"] <= global_control["total_quantity"]):
                raise ValueError("control total mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            raise SemanticRuntimeError("渠道统计未通过完整性对账，未展示不可靠结果", reason="price_group_reconciliation_failed") from exc
        window = f"{request.start.isoformat()} 00:00 至 {request.end.isoformat()} 00:00（不含结束时点，北京时间）"
        summary = [] if not rows else [
            {"label":"实际销售出库","value":format(global_control["total_quantity"],"f"),"unit":"EA"},
            {"label":"出库单数","value":str(global_control["total_outbound_count"]),"unit":"单"},
            {"label":"有效 SKU","value":str(global_control["total_sku_count"]),"unit":"个"},
        ]
        table = _display_table(table_id="sales_price_group",title="渠道与履约类型销售出库",caption=window,
            columns=[{"key":k,"kind":"text"} for k in ("channel_code","channel_name","fulfillment_type")]
                    + [{"key":k,"kind":"number"} for k in ("quantity","outbound_count","sku_count")],
            rows=displayed,summary=summary)
        table["notes"] = ["SKU总数为全范围去重，不是各组SKU数相加；零数量有效出库单仍计入单数。",
                          "渠道按当前客户主数据归属；未匹配渠道和未配置履约类型保留，不自动归类。"]
        if request.sales_aggregation and request.sales_aggregation.order_types:
            table["notes"][0] += "本次出库类型范围：" + "、".join(request.sales_aggregation.order_types) + "。"
        if rows:
            table["notes"].append(f"最近出库时间：{global_control['latest_shipment_at']}；客户主数据最近修改时间：{global_control['customer_updated_at']}。不代表同步水位。")
        text = ("已按渠道细分和2C/2B完成汇总，数量与出库单数均已对账。" if rows else "查询成功，当前范围没有有效销售出库记录。")
        return SemanticAnswer(f"{text}{window}。范围：{request.scope.label()}。",table)
