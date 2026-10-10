"""Candidate disjoint channel aggregation; no default for disputed overlap rules."""

from collections import defaultdict
from datetime import date
from fractions import Fraction

import sqlglot
from sqlglot import exp

from ..query_scope import QueryScope, apply_scope
from .sales_aggregation import SalesAggregation, compile_sales_aggregation
from .semantic_types import _parse_decimal, _parse_source_datetime


PLATFORMS = {
    "抖音": ("DY", "WXSPHXD"),
    "抖音小时达": ("DYXD",),
    "拼多多系": ("PDD", "PDDDF"),
    "快手系": ("KS", "KSDF"),
    "淘宝系": ("TGC", "MJYX", "TB", "CT", "MAOCHAO"),
    "京东系": ("JDVMI", "JD", "JD_MARKET"),
    "唯品会": ("WPH",),
}
OVERLAP_POLICIES = {"platform": "社区团购平台", "distribution": "社区团购分销", "separate": "平台与分销重叠"}


def compile_channel_breakdown(*, start: date | None, end: date | None,
                              scope: QueryScope, overlap_policy: str) -> str:
    if overlap_policy not in OVERLAP_POLICIES:
        raise ValueError("explicit channel overlap policy required")
    if scope.channel != "ALL" or scope.unit != "EA":
        raise ValueError("unreviewed channel measure or filter")
    base = sqlglot.parse_one(apply_scope(compile_sales_aggregation(
        SalesAggregation(("customer_channel",)), start=start, end=end
    ), scope, legacy_owner="HJXH"), read="doris")
    common = base.args["with_"]
    # Reuse the reviewed scoped fact set, replacing only the customer dimension.
    # Distinct whole tuples avoid both join fanout and fictional MIN-field records.
    customers = sqlglot.parse_one("""WITH customer_attributes AS (
 SELECT DISTINCT TRIM(cs.company_code) AS cargo_owner,TRIM(cs.code) AS store_code,
   NULLIF(TRIM(cs.type),'') AS customer_type,NULLIF(TRIM(cs.platform_code),'') AS platform_code,
   NULLIF(TRIM(cs.group1),'') AS group1,NULLIF(TRIM(cs.price_group),'') AS price_group
 FROM ywzt_ods.master_store cs
 WHERE TRIM(cs.company_code)<>'' AND TRIM(cs.code)<>''
 AND EXISTS (SELECT 1 FROM headers h WHERE h.cargo_owner=TRIM(cs.company_code) AND h.store_code=TRIM(cs.code))
), customer_types AS (
 SELECT cargo_owner,store_code,MIN(customer_type) AS customer_type,
   MIN(platform_code) AS platform_code,MIN(group1) AS group1,MIN(price_group) AS price_group,
   COUNT(*) AS type_variants
 FROM customer_attributes GROUP BY cargo_owner,store_code
) SELECT 1""", read="doris").args["with_"].expressions
    online_cases = " ".join(
        "WHEN ct.platform_code IN (" + ",".join(exp.Literal.string(code).sql(dialect="doris") for code in codes)
        + ") THEN " + exp.Literal.string(label).sql(dialect="doris")
        for label, codes in PLATFORMS.items()
    )
    known = ",".join(exp.Literal.string(code).sql(dialect="doris") for codes in PLATFORMS.values() for code in codes)
    overlap = "ct.customer_type='Z001' AND ct.group1='03' AND ct.price_group='122'"
    projection = sqlglot.parse_one(f"""SELECT
 CASE WHEN ct.customer_type='Z004' THEN CASE {online_cases} ELSE '线上其他' END
      WHEN ct.customer_type='Z001' THEN CASE
        WHEN {overlap} THEN '{OVERLAP_POLICIES[overlap_policy]}'
        WHEN ct.group1='03' THEN '社区团购平台'
        WHEN ct.price_group='122' THEN '社区团购分销' ELSE '线下其他' END
      ELSE '其他' END AS channel_detail,
 CASE WHEN {overlap} THEN 1 ELSE 0 END AS channel_overlap,
 CASE WHEN ct.customer_type='Z004' AND ct.platform_code IS NULL THEN 1 ELSE 0 END AS blank_platform,
 CASE WHEN ct.customer_type='Z004' AND ct.platform_code IS NOT NULL
   AND ct.platform_code NOT IN ({known}) THEN 1 ELSE 0 END AS unknown_platform
""", read="doris").expressions
    retained = []
    for cte in common.expressions:
        if cte.alias_or_name == "customer_types":
            retained.extend(customers)
        elif cte.alias_or_name in {"header_variants", "headers", "detail_by_sku", "facts", "controls", "header_controls", "all_documents"}:
            if cte.alias_or_name == "facts":
                cte.this.set("expressions", [*cte.this.expressions, *projection])
            retained.append(cte)
    suffix = sqlglot.parse_one("""WITH channel_leaves AS (
 SELECT customer_channel,channel_detail,SUM(quantity) AS quantity
 FROM facts GROUP BY customer_channel,channel_detail
), leaf_document_keys AS (
 SELECT DISTINCT customer_channel,channel_detail,cargo_owner,outbound_code FROM facts
), leaf_documents AS (
 SELECT customer_channel,channel_detail,COUNT(*) AS outbound_count
 FROM leaf_document_keys GROUP BY customer_channel,channel_detail
), channel_totals AS (
 SELECT customer_channel,SUM(quantity) AS channel_quantity FROM facts GROUP BY customer_channel
), channel_document_keys AS (
 SELECT DISTINCT customer_channel,cargo_owner,outbound_code FROM facts
), channel_documents AS (
 SELECT customer_channel,COUNT(*) AS channel_outbound_count FROM channel_document_keys GROUP BY customer_channel
), overlap_document_keys AS (
 SELECT DISTINCT cargo_owner,outbound_code FROM facts WHERE channel_overlap=1
), missing_customer_document_keys AS (
 SELECT DISTINCT cargo_owner,outbound_code FROM facts WHERE missing_customer=1
), blank_platform_document_keys AS (
 SELECT DISTINCT cargo_owner,outbound_code FROM facts WHERE blank_platform=1
), unknown_platform_document_keys AS (
 SELECT DISTINCT cargo_owner,outbound_code FROM facts WHERE unknown_platform=1
), channel_diagnostics AS (
 SELECT COALESCE(SUM(CASE WHEN channel_overlap=1 THEN quantity ELSE 0 END),0) AS overlap_quantity,
   COALESCE(SUM(CASE WHEN blank_platform=1 THEN quantity ELSE 0 END),0) AS blank_platform_quantity,
   COALESCE(SUM(CASE WHEN unknown_platform=1 THEN quantity ELSE 0 END),0) AS unknown_platform_quantity
 FROM facts
)
SELECT l.customer_channel,l.channel_detail,l.quantity,d.outbound_count,
 t.channel_quantity,cd.channel_outbound_count,c.*,hc.header_conflict_count,x.*,
 (SELECT COUNT(*) FROM overlap_document_keys) AS overlap_documents,
 (SELECT COUNT(*) FROM missing_customer_document_keys) AS missing_customer_documents,
 (SELECT COUNT(*) FROM blank_platform_document_keys) AS blank_platform_documents,
 (SELECT COUNT(*) FROM unknown_platform_document_keys) AS unknown_platform_documents,
 (SELECT COUNT(*) FROM all_documents) AS total_outbound_count,
 (SELECT COUNT(*) FROM channel_leaves) AS group_count
FROM channel_leaves l
JOIN leaf_documents d ON d.customer_channel=l.customer_channel AND d.channel_detail=l.channel_detail
JOIN channel_totals t ON t.customer_channel=l.customer_channel
JOIN channel_documents cd ON cd.customer_channel=l.customer_channel
CROSS JOIN controls c CROSS JOIN header_controls hc CROSS JOIN channel_diagnostics x
ORDER BY l.customer_channel,l.channel_detail LIMIT 501""", read="doris")
    common.set("expressions", [*retained, *suffix.args.pop("with_").expressions])
    suffix.set("with_", common)
    return suffix.sql(dialect="doris")


def _count(value):
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError("invalid channel count")
    return int(number)


def reconcile_channels(rows, *, overlap_policy: str):
    if overlap_policy not in OVERLAP_POLICIES:
        raise ValueError("explicit channel overlap policy required")
    if len(rows) > 500:
        raise ValueError("channel result truncated")
    allowed = {"线上": {*PLATFORMS, "线上其他"}, "线下": {"社区团购平台", "社区团购分销", "线下其他"}, "其他": {"其他"}}
    if overlap_policy == "separate":
        allowed["线下"].add(OVERLAP_POLICIES[overlap_policy])
    # Rational sums preserve source decimals independently of ambient precision.
    amounts, documents, parent_controls = defaultdict(Fraction), defaultdict(int), {}
    keys, result, global_controls = set(), [], None
    decimal_controls = ("total_quantity", "missing_customer_quantity", "overlap_quantity", "blank_platform_quantity", "unknown_platform_quantity")
    for row in rows:
        channel, detail = row.get("customer_channel"), row.get("channel_detail")
        if channel not in allowed or detail not in allowed[channel] or (channel,detail) in keys:
            raise ValueError("invalid or duplicate channel key")
        keys.add((channel,detail))
        values = {key:Fraction(_parse_decimal(row[key])) for key in decimal_controls}
        values.update({key:_count(row[key]) for key in ("total_outbound_count","overlap_documents","missing_customer_documents","blank_platform_documents","unknown_platform_documents")})
        values["latest_shipment_at"] = row["latest_shipment_at"]
        _parse_source_datetime(values["latest_shipment_at"],timezone_name="Asia/Shanghai")
        if any(values[key] < 0 for key in decimal_controls) or _count(row["group_count"]) != len(rows):
            raise ValueError("invalid channel control")
        if global_controls is None:
            global_controls = values
        elif global_controls != values:
            raise ValueError("inconsistent channel control")
        if any(_count(row[key]) for key in ("customer_conflict_count","header_conflict_count","null_quantity_count","negative_quantity_count","blank_key_count")):
            raise ValueError("conflicting or invalid channel facts")
        quantity, count = _parse_decimal(row["quantity"]), _count(row["outbound_count"])
        parent = (_parse_decimal(row["channel_quantity"]),_count(row["channel_outbound_count"]))
        if quantity < 0 or not 0 < count <= parent[1] <= values["total_outbound_count"] or not quantity <= parent[0] <= values["total_quantity"]:
            raise ValueError("invalid channel totals")
        if channel in parent_controls and parent_controls[channel] != parent:
            raise ValueError("inconsistent channel parent")
        parent_controls[channel] = parent
        amounts[channel] += Fraction(quantity)
        documents[channel] += count
        result.append({"customer_channel":channel,"channel_detail":detail,"quantity":format(quantity,"f"),"outbound_count":count})
    if not rows:
        return []
    if any((amounts[channel],documents[channel]) != control for channel,control in parent_controls.items()):
        raise ValueError("channel subtotal mismatch")
    if sum(amounts.values()) != global_controls["total_quantity"] or sum(documents.values()) != global_controls["total_outbound_count"]:
        raise ValueError("channel company control mismatch")
    leaf_amounts = {(row["customer_channel"],row["channel_detail"]):Fraction(row["quantity"]) for row in result}
    overlap_leaf = ("线下",OVERLAP_POLICIES[overlap_policy])
    overlap_docs = next((row["outbound_count"] for row in result if (row["customer_channel"],row["channel_detail"])==overlap_leaf),0)
    if global_controls["overlap_quantity"] > leaf_amounts.get(overlap_leaf,0) or global_controls["overlap_documents"] > overlap_docs:
        raise ValueError("invalid channel overlap impact")
    if overlap_policy == "separate" and (global_controls["overlap_quantity"] != leaf_amounts.get(overlap_leaf,0) or global_controls["overlap_documents"] != overlap_docs):
        raise ValueError("separate overlap control mismatch")
    if global_controls["missing_customer_quantity"] > amounts["其他"] or global_controls["blank_platform_quantity"] + global_controls["unknown_platform_quantity"] > leaf_amounts.get(("线上","线上其他"),0):
        raise ValueError("invalid missing channel impact")
    online_other_docs = next((row["outbound_count"] for row in result if (row["customer_channel"],row["channel_detail"])==("线上","线上其他")),0)
    if global_controls["missing_customer_documents"] > documents["其他"] or global_controls["blank_platform_documents"] + global_controls["unknown_platform_documents"] > online_other_docs:
        raise ValueError("invalid missing channel document impact")
    if any(global_controls[quantity] > 0 and global_controls[count] == 0 for quantity,count in (
        ("missing_customer_quantity","missing_customer_documents"), ("blank_platform_quantity","blank_platform_documents"),
        ("unknown_platform_quantity","unknown_platform_documents"), ("overlap_quantity","overlap_documents")
    )):
        raise ValueError("channel impact has no supporting documents")
    return result
