"""Candidate name provenance checks, independent of actual-outbound measures."""

from datetime import date
from decimal import Decimal

import sqlglot
from sqlglot import exp

from ..query_scope import QueryScope, apply_scope
from .sales_aggregation import SalesAggregation, compile_sales_aggregation
from .semantic_types import _parse_decimal


SOURCES = ("master", "shipment", "order")


def compile_sku_name_quality(*, start: date | None, end: date | None, scope: QueryScope) -> str:
    if scope.channel != "ALL" or scope.unit != "EA":
        raise ValueError("unreviewed name-quality measure or channel")
    scoped_facts = apply_scope(compile_sales_aggregation(
        SalesAggregation(("cargo_owner", "sku")), start=start, end=end
    ), scope, legacy_owner="HJXH")
    base = sqlglot.parse_one(scoped_facts, read="doris")
    common = base.args.pop("with_")
    base.set("limit", None)
    base.set("order", None)
    common.append("expressions", exp.CTE(this=base, alias=exp.TableAlias(this=exp.to_identifier("sku_sales"))))
    stats = []
    for source in SOURCES:
        stats.extend([
            f"SUM(CASE WHEN origin='{source}' THEN 1 ELSE 0 END) AS {source}_rows",
            f"SUM(CASE WHEN origin='{source}' AND name IS NULL THEN 1 ELSE 0 END) AS {source}_missing",
            f"COUNT(DISTINCT CASE WHEN origin='{source}' THEN name END) AS {source}_variants",
            f"CASE WHEN COUNT(DISTINCT CASE WHEN origin='{source}' THEN name END)=1 THEN MIN(CASE WHEN origin='{source}' THEN name END) END AS {source}_name",
        ])
    # EXISTS selects observations by qualified fact keys. Never join raw name
    # rows to quantities, or count one order again for every shipment it produced.
    suffix = sqlglot.parse_one(f"""WITH name_observations AS (
 SELECT s.cargo_owner,s.sku_code,'master' AS origin,NULLIF(TRIM(m.sku_name),'') AS name
 FROM sku_sales s JOIN ywzt_ods.master_sku m ON TRIM(m.sku_code)=s.sku_code
 UNION ALL
 SELECT TRIM(d.company_code),TRIM(d.sku_code),'shipment',NULLIF(TRIM(d.sku_name),'')
 FROM ywzt_ods.scm_sales_shipment_detail d
 WHERE COALESCE(d.delete_flag,0)=0 AND IFNULL(d._fdl_marked_deleted,'false')='false'
 AND EXISTS (SELECT 1 FROM facts f WHERE f.cargo_owner=TRIM(d.company_code)
   AND f.outbound_code=TRIM(d.outbound_code) AND f.sku_code=TRIM(d.sku_code))
 UNION ALL
 SELECT TRIM(d.company_code),TRIM(d.sku_code),'order',NULLIF(TRIM(d.sku_name),'')
 FROM ywzt_ods.scm_sales_order_detail d
 WHERE IFNULL(d._fdl_marked_deleted,'false')='false'
 AND EXISTS (SELECT 1 FROM header_variants h JOIN facts f
   ON f.cargo_owner=h.cargo_owner AND f.outbound_code=h.outbound_code
   WHERE h.cargo_owner=TRIM(d.company_code) AND h.sales_order_code=TRIM(d.sales_order_code)
   AND f.sku_code=TRIM(d.sku_code))
), name_stats AS (
 SELECT cargo_owner,sku_code,{','.join(stats)},COUNT(DISTINCT name) AS all_name_variants
 FROM name_observations GROUP BY cargo_owner,sku_code
)
SELECT s.*,{','.join(f'n.{source}_{field}' for source in SOURCES for field in ('rows','missing','variants','name'))},
 n.all_name_variants
FROM sku_sales s JOIN name_stats n ON n.cargo_owner=s.cargo_owner AND n.sku_code=s.sku_code
ORDER BY s.cargo_owner,s.sku_code LIMIT 501""", read="doris")
    common.set("expressions", [*common.expressions, *suffix.args.pop("with_").expressions])
    suffix.set("with_", common)
    # Observe all names for the selected fact keys, not just names surviving
    # product-category or name filters that could hide conflicting master rows.
    return suffix.sql(dialect="doris")


def _count(value):
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError("invalid name-quality count")
    return int(number)


def reconcile_sku_names(rows):
    if len(rows) > 500:
        raise ValueError("name-quality result truncated")
    result, keys = [], set()
    total, documents, timestamp = None, None, None
    quantity_sum = Decimal(0)
    for row in rows:
        key = (row.get("cargo_owner"), row.get("sku_code"))
        if any(not isinstance(value,str) or not value.strip() for value in key) or key in keys:
            raise ValueError("missing or duplicate name-quality key")
        keys.add(key)
        quantity = _parse_decimal(row["quantity"])
        current_total = _parse_decimal(row["total_quantity"])
        current_documents = _count(row["total_outbound_count"])
        count = _count(row["outbound_count"])
        if total is None:
            total, documents, timestamp = current_total, current_documents, row["latest_shipment_at"]
        if quantity < 0 or count < 1 or count > documents or _count(row["group_count"]) != len(rows):
            raise ValueError("invalid name-quality fact control")
        if current_total != total or current_documents != documents or row["latest_shipment_at"] != timestamp or not timestamp:
            raise ValueError("inconsistent name-quality fact control")
        if any(_count(row[field]) for field in ("null_quantity_count","negative_quantity_count","blank_key_count","header_conflict_count")):
            raise ValueError("invalid underlying sales facts")
        item = {"cargo_owner":key[0],"sku_code":key[1],"quantity":format(quantity,"f"),"outbound_count":count}
        missing, variant_counts, unique_names = False, [], set()
        for source in SOURCES:
            records, blank, variants = (_count(row[f"{source}_{field}"]) for field in ("rows","missing","variants"))
            name = row[f"{source}_name"]
            if blank > records or variants > records-blank or (variants == 0) != (records == blank):
                raise ValueError("inconsistent source name counts")
            if (variants == 1 and (not isinstance(name,str) or not name.strip())) or (variants != 1 and name is not None):
                raise ValueError("invalid unique source name")
            if variants == 1:
                unique_names.add(name)
            missing |= records == 0 or blank > 0
            variant_counts.append(variants)
            item.update({f"{source}_rows":records,f"{source}_missing":blank,f"{source}_variants":variants,f"{source}_name":name})
        all_variants = _count(row["all_name_variants"])
        if not max(variant_counts) <= all_variants <= sum(variant_counts) or len(unique_names) > all_variants:
            raise ValueError("inconsistent cross-source variants")
        item.update(sku_name=item["master_name"] or key[1],
                    name_source="master_sku" if item["master_name"] else "sku_code",
                    name_conflict=all_variants > 1,name_missing=missing,all_name_variants=all_variants)
        result.append(item)
        quantity_sum += quantity
    if rows and quantity_sum != total:
        raise ValueError("name-quality quantity control mismatch")
    return result
