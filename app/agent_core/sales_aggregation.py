"""One sales fact set with independent quantity and document-count aggregates."""

from dataclasses import dataclass
from datetime import date

from sqlglot import exp

from ..outbound_contract import completed_status_sql, ref_order_type_sql


DIMENSION_KEYS = {
    "ship_date": ("ship_date",),
    "cargo_owner": ("cargo_owner",),
    "warehouse": ("cargo_owner", "warehouse_code"),
    "shipment_status": ("shipment_status",),
    "ref_order_type": ("ref_order_type",),
    "fulfillment_type": ("fulfillment_type",),
    "sku": ("sku_code",),
    "customer_channel": ("customer_channel",),
}


@dataclass(frozen=True, slots=True)
class SalesAggregation:
    dimensions: tuple[str, ...] = ()
    ref_order_type: str | None = None
    fulfillment_type: str | None = None
    ref_order_types: tuple[str, ...] = ()
    include_sku_count: bool = False
    include_zero_document_count: bool = False
    channel_filter: str | None = None

    def __post_init__(self):
        if len(set(self.dimensions)) != len(self.dimensions) or any(d not in DIMENSION_KEYS for d in self.dimensions):
            raise ValueError("unsupported sales dimension")
        if (self.ref_order_type is not None and self.ref_order_types
                or not isinstance(self.ref_order_types, tuple)
                or len(set(self.ref_order_types)) != len(self.ref_order_types)):
            raise ValueError("ambiguous sales order types")
        if any(not isinstance(value, str) or not (
            value in {"JYCK", "HHCK", "BFCK", "QTCK"}
            or value.startswith("SO")
        ) for value in self.order_types):
            raise ValueError("unsupported sales order type")
        if type(self.include_sku_count) is not bool or type(self.include_zero_document_count) is not bool:
            raise ValueError("sales count flags must be boolean")
        if self.fulfillment_type not in {None, "2C", "2B"}:
            raise ValueError("unsupported fulfillment type")
        if self.channel_filter not in {None, "线上", "线下", "其他"}:
            raise ValueError("unsupported customer channel")
        if self.channel_filter is not None and "customer_channel" not in self.dimensions:
            raise ValueError("customer channel filter requires its dimension")

    @property
    def order_types(self):
        return self.ref_order_types or ((self.ref_order_type,) if self.ref_order_type is not None else ())

    @property
    def keys(self):
        return tuple(dict.fromkeys(key for dimension in self.dimensions for key in DIMENSION_KEYS[dimension]))


def compile_sales_aggregation(plan: SalesAggregation, *, start: date | None, end: date | None,
                              finished_goods_only: bool = False,
                              all_outbound_types: bool = False) -> str:
    if (start is None) != (end is None) or (start is not None and end <= start):
        raise ValueError("sales interval must be nonempty")
    time_filter = "TRUE" if start is None else f"h.ship_at >= '{start.isoformat()} 00:00:00' AND h.ship_at < '{end.isoformat()} 00:00:00'"
    keys = plan.keys
    customer_channel = "customer_channel" in plan.dimensions
    key_sql = ", ".join(keys)
    group_sql = f"GROUP BY {key_sql}" if keys else ""
    document_keys = ", ".join(dict.fromkeys((*keys, "cargo_owner", "outbound_code")))
    key_projection = f"{key_sql}, " if keys else ""
    join_keys = " AND ".join(f"q.{k} <=> n.{k}" for k in keys) or "TRUE"
    output_keys = ", ".join(f"q.{k}" for k in keys)
    output_prefix = output_keys + ", " if keys else ""
    order_sql = output_keys if keys else "q.quantity DESC"
    type_filter = ""
    if plan.order_types:
        literals = ", ".join(exp.Literal.string(value).sql(dialect="doris") for value in plan.order_types)
        type_filter = f" AND TRIM(h.ref_order_type) IN ({literals})"
    if plan.fulfillment_type:
        operator = "=" if plan.fulfillment_type == "2C" else "<>"
        type_filter += f" AND NULLIF(TRIM(CAST(h.terms_of_shipment AS STRING)), '') {operator} '20'"
    finished_sku_cte = """finished_skus AS (
  SELECT DISTINCT TRIM(sku_code) AS sku_code
  FROM ywzt_ods.master_sku
  WHERE TRIM(CAST(sku_group AS STRING)) = '50'
    AND TRIM(sku_code) <> ''
),""" if finished_goods_only else ""
    finished_sku_join = "JOIN finished_skus fs ON fs.sku_code = TRIM(d.sku_code)" if finished_goods_only else ""
    channel_predicate = "TRUE"
    if plan.channel_filter:
        channel_predicate = (
            "CASE WHEN ct.customer_type = 'Z004' THEN '线上' "
            "WHEN ct.customer_type = 'Z001' THEN '线下' ELSE '其他' END = "
            f"{exp.Literal.string(plan.channel_filter).sql(dialect='doris')}"
        )
        # Conflicting classifications must reach the quality control even if
        # MIN happens to put the customer outside the requested channel.
        channel_predicate = f"({channel_predicate} OR ct.type_variants > 1)"
    outbound_type_predicate = (
        "(TRIM(h.ref_order_type) IN ('JYCK', 'HHCK', 'BFCK', 'QTCK') "
        "OR TRIM(h.ref_order_type) LIKE 'SO%' OR TRIM(h.ref_order_type) = 'DBCK')"
        if all_outbound_types else ref_order_type_sql()
    )
    order_link_predicate = (
        "AND (TRIM(h.ref_order_type) = 'DBCK' OR EXISTS ("
        "SELECT 1 FROM ywzt_ods.scm_sales_order_header AS so "
        "WHERE TRIM(so.company_code) = TRIM(h.company_code) "
        "AND TRIM(so.sales_order_code) = TRIM(h.ref_order_code) "
        "AND TRIM(so.sales_order_code) <> '' "
        "AND COALESCE(so.deleted_flag, 0) = 0 "
        "AND IFNULL(so._fdl_marked_deleted, 'false') = 'false'))"
        if all_outbound_types else
        "AND EXISTS ("
        "SELECT 1 FROM ywzt_ods.scm_sales_order_header AS so "
        "WHERE TRIM(so.company_code) = TRIM(h.company_code) "
        "AND TRIM(so.sales_order_code) = TRIM(h.ref_order_code) "
        "AND TRIM(so.sales_order_code) <> '' "
        "AND COALESCE(so.deleted_flag, 0) = 0 "
        "AND IFNULL(so._fdl_marked_deleted, 'false') = 'false')"
    )
    # Group by normalized business keys before joining facts. Conflicting header
    # attributes are detected explicitly; MIN is never permission to hide them.
    return f"""WITH header_variants AS (
  SELECT DISTINCT TRIM(h.company_code) AS cargo_owner,
    TRIM(h.outbound_code) AS outbound_code, TRIM(h.facility_code) AS warehouse_code,
    h.ship_at, CAST(h.status AS STRING) AS shipment_status,
    {"TRIM(h.store_code) AS store_code," if customer_channel else ""}
    TRIM(h.ref_order_type) AS ref_order_type, TRIM(h.ref_order_code) AS sales_order_code,
    CASE WHEN TRIM(CAST(h.terms_of_shipment AS STRING)) = '20' THEN '2C'
         WHEN NULLIF(TRIM(CAST(h.terms_of_shipment AS STRING)), '') IS NULL THEN '未配置'
         ELSE '2B' END AS fulfillment_type
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE {time_filter}
    AND {completed_status_sql()} AND {outbound_type_predicate}{type_filter}
    AND COALESCE(h.delete_flag, 0) = 0
    AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
    {order_link_predicate}
), headers AS (
  SELECT cargo_owner, outbound_code, MIN(warehouse_code) AS warehouse_code,
    MIN(ship_at) AS ship_at, MIN(shipment_status) AS shipment_status, MIN(ref_order_type) AS ref_order_type,
    MIN(fulfillment_type) AS fulfillment_type, COUNT(*) AS header_variants
    {", MIN(store_code) AS store_code" if customer_channel else ""}
  FROM header_variants GROUP BY cargo_owner, outbound_code
), {finished_sku_cte} detail_by_sku AS (
  SELECT TRIM(d.company_code) AS cargo_owner, TRIM(d.outbound_code) AS outbound_code,
    TRIM(d.sku_code) AS sku_code, SUM(COALESCE(d.ship_qty, 0)) AS quantity,
    COUNT(*) AS detail_count,
    SUM(CASE WHEN d.ship_qty IS NULL THEN 1 ELSE 0 END) AS null_quantity_count,
    SUM(CASE WHEN d.ship_qty < 0 THEN 1 ELSE 0 END) AS negative_quantity_count
  FROM ywzt_ods.scm_sales_shipment_detail AS d
  {finished_sku_join}
  WHERE COALESCE(d.delete_flag, 0) = 0
    AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
    AND EXISTS (SELECT 1 FROM headers AS h WHERE h.cargo_owner = TRIM(d.company_code)
      AND h.outbound_code = TRIM(d.outbound_code))
  GROUP BY TRIM(d.company_code), TRIM(d.outbound_code), TRIM(d.sku_code)
){", customer_types AS (SELECT TRIM(cs.company_code) AS cargo_owner, TRIM(cs.code) AS store_code, MIN(NULLIF(TRIM(cs.type), '')) AS customer_type, COUNT(DISTINCT COALESCE(NULLIF(TRIM(cs.type), ''), '<missing>')) AS type_variants FROM ywzt_ods.master_store cs WHERE TRIM(cs.code) <> '' AND TRIM(cs.company_code) <> '' AND EXISTS (SELECT 1 FROM headers h WHERE h.cargo_owner = TRIM(cs.company_code) AND h.store_code = TRIM(cs.code)) GROUP BY TRIM(cs.company_code), TRIM(cs.code))" if customer_channel else ""}, facts AS (
  SELECT h.cargo_owner, h.outbound_code, h.warehouse_code, DATE(h.ship_at) AS ship_date,
    h.shipment_status, h.ref_order_type, h.fulfillment_type, h.ship_at, h.header_variants,
    d.sku_code, d.quantity, d.detail_count, d.null_quantity_count, d.negative_quantity_count
    {", CASE WHEN ct.customer_type = 'Z004' THEN '线上' WHEN ct.customer_type = 'Z001' THEN '线下' ELSE '其他' END AS customer_channel, COALESCE(ct.type_variants, 0) AS customer_type_variants, CASE WHEN ct.store_code IS NULL OR ct.customer_type IS NULL THEN 1 ELSE 0 END AS missing_customer" if customer_channel else ""}
  FROM headers AS h
  JOIN detail_by_sku AS d ON d.cargo_owner = h.cargo_owner AND d.outbound_code = h.outbound_code
  {"LEFT JOIN customer_types AS ct ON ct.cargo_owner = h.cargo_owner AND ct.store_code = h.store_code" if customer_channel else ""}
  WHERE {channel_predicate}
), quantities AS (
  SELECT {key_projection}SUM(quantity) AS quantity,
    COUNT(DISTINCT sku_code) AS sku_count FROM facts {group_sql}
), group_documents AS (
  SELECT {document_keys}, SUM(quantity) AS document_quantity
  FROM facts GROUP BY {document_keys}
), document_counts AS (
  SELECT {key_projection}COUNT(*) AS outbound_count,
    SUM(CASE WHEN document_quantity = 0 THEN 1 ELSE 0 END) AS zero_outbound_count
  FROM group_documents {group_sql}
), all_documents AS (
  SELECT DISTINCT cargo_owner, outbound_code FROM facts
), controls AS (
  SELECT COALESCE(SUM(quantity), 0) AS total_quantity,
    COUNT(DISTINCT sku_code) AS total_sku_count,
    COALESCE(SUM(null_quantity_count), 0) AS null_quantity_count,
    COALESCE(SUM(negative_quantity_count), 0) AS negative_quantity_count,
    MAX(ship_at) AS latest_shipment_at,
    COALESCE(SUM(CASE WHEN COALESCE(cargo_owner, '') = '' OR COALESCE(outbound_code, '') = ''
      OR COALESCE(sku_code, '') = '' THEN 1 ELSE 0 END), 0) AS blank_key_count
    {", COALESCE(SUM(CASE WHEN customer_type_variants > 1 THEN 1 ELSE 0 END), 0) AS customer_conflict_count, COALESCE(SUM(CASE WHEN missing_customer = 1 THEN quantity ELSE 0 END), 0) AS missing_customer_quantity" if customer_channel else ""}
  FROM facts
), header_controls AS (
  SELECT COALESCE(SUM(CASE WHEN header_variants > 1 THEN 1 ELSE 0 END), 0) AS header_conflict_count
  FROM headers h WHERE EXISTS (SELECT 1 FROM facts f WHERE f.cargo_owner=h.cargo_owner AND f.outbound_code=h.outbound_code)
), wms_names AS (
  SELECT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner, TRIM(wms_code) AS warehouse_code,
    MIN(NULLIF(TRIM(wms_desc), '')) AS warehouse_name,
    COUNT(DISTINCT NULLIF(TRIM(wms_desc), '')) AS warehouse_name_variants
  FROM ywzt_ods.master_wms WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
  GROUP BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(wms_code)
), summary AS (
  SELECT {output_prefix}q.quantity, q.sku_count, n.outbound_count
    {", n.zero_outbound_count" if plan.include_zero_document_count else ""}
  FROM quantities AS q JOIN document_counts AS n ON {join_keys}
  WHERE n.outbound_count > 0
)
SELECT s.*, c.total_quantity, c.total_sku_count, c.null_quantity_count, c.negative_quantity_count, c.blank_key_count,
  c.latest_shipment_at, hc.header_conflict_count,
  (SELECT COUNT(*) FROM all_documents) AS total_outbound_count,
  (SELECT COUNT(*) FROM summary) AS group_count
  {", c.customer_conflict_count, c.missing_customer_quantity" if customer_channel else ""}
  {", COALESCE(w.warehouse_name, s.warehouse_code) AS warehouse_name, COALESCE(w.warehouse_name_variants, 0) AS warehouse_name_variants" if "warehouse" in plan.dimensions else ""}
FROM summary AS s CROSS JOIN controls AS c CROSS JOIN header_controls AS hc
{"LEFT JOIN wms_names AS w ON w.cargo_owner = s.cargo_owner AND w.warehouse_code = s.warehouse_code" if "warehouse" in plan.dimensions else ""}
ORDER BY {order_sql.replace("q.", "s.")} LIMIT 100001"""
