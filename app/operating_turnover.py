"""Governed SQL for the Royal Little Tiger operating-center turnover report.

The legacy turnover builder remains available for compatibility with older
contracts.  This module contains the reviewed, reference-style profile: eight
fixed operating centers, explicit warehouse-code hierarchy resolution, and the
current Doris ``sku_inventory`` snapshot as the default inventory source.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .turnover_template import TurnoverTemplate

from .turnover_report import (
    DEFAULT_TIMEZONE,
    OPERATING_CENTER_CODES,
    TURNOVER_WINDOW_DAYS,
    TurnoverReportError,
    _sql_literal,
    _window_bounds,
)
from .outbound_contract import completed_status_sql, ref_order_type_sql
from .inventory_source import bind_current_inventory
from .sales_source import bind_business_sales
from .query_scope import QueryScope


OPERATING_CENTER_DEFINITIONS = (
    ("WMS000005", "皇家小虎成都-万纬仓", 1),
    ("9813421", "皇家小虎合肥万纬仓xs", 2),
    ("9660544", "皇家小虎济南-万维仓xs", 3),
    ("TJWWC-777", "皇家小虎天津-万纬仓", 4),
    ("WMS000019", "皇家小虎河北石家庄仓xs", 5),
    ("10114790", "皇家小虎长沙黄兴万纬仓xs", 6),
    ("WMS000232", "皇家小虎沈阳-鲜生活仓xs", 7),
    ("WMS000245", "皇家小虎郑州-万纬仓xs", 8),
)

# These active master rows have no matching finished-goods facts in either
# governed inventory source at the time this profile was reviewed.  Treating
# absence as zero would make a plausible but unprovable report.  Keep the
# exclusion explicit; a future fact on one of these codes is a contract-drift
# signal that must be reviewed before it is included.
OPERATING_CENTER_EXCLUDED_WAREHOUSE_CODES = ("WMS000008", "WMS000148")
OPERATING_CENTER_ALLOWED_WMS_TYPES = (10, 11, 14)

if tuple(item[0] for item in OPERATING_CENTER_DEFINITIONS) != OPERATING_CENTER_CODES:
    raise RuntimeError("operating-center definitions do not match the reviewed report order")


def _quoted(value: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        raise TurnoverReportError("profile value contains unsupported characters")
    return "'" + text.replace("'", "''") + "'"


def _union_literals(values: Sequence[tuple[str, str, int]]) -> str:
    return " UNION ALL ".join(
        f"SELECT {_quoted(code)} AS center_code, '{name.replace(chr(39), chr(39) * 2)}' AS expected_name, {order_no} AS center_order"
        for code, name, order_no in values
    )


def _union_codes(values: Sequence[str], alias: str) -> str:
    if not values:
        raise TurnoverReportError(f"{alias} cannot be empty")
    return " UNION ALL ".join(
        f"SELECT {_quoted(value)} AS {alias}" if index == 0 else f"SELECT {_quoted(value)}"
        for index, value in enumerate(values)
    )


def _normalise_basis(value: Any) -> str:
    basis = str(value or "").strip().casefold()
    aliases = {
        "available_plus_transit": "available_plus_transit",
        "available_plus_transit_finished_goods": "available_plus_transit",
        "available_transit": "available_plus_transit",
        "actual_quantity": "actual_quantity",
        "record_deductions": "record_deductions",
    }
    try:
        return aliases[basis]
    except KeyError as exc:
        raise TurnoverReportError(
            "inventory_basis must be available_plus_transit, actual_quantity, or record_deductions"
        ) from exc


def build_operating_center_turnover_sql(
    start_inclusive: Any,
    end_exclusive: Any,
    cargo_owner_id: str = "HJXH",
    *,
    inventory_basis: str = "available_plus_transit",
    inventory_status: str = "ALL",
    occupation_lookback_days: int = 60,
    timezone_name: str = DEFAULT_TIMEZONE,
    window_days: int = TURNOVER_WINDOW_DAYS,
    warehouse_template: TurnoverTemplate | None = None,
) -> str:
    """Build one read-only Doris statement for the fixed eight-center profile.

    The statement deliberately returns only warehouse aggregates and repeated
    quality counters.  It does not return SKU rows, credentials, or audit
    contents to the browser.
    """

    start_dt, end_dt, _ = _window_bounds(
        start_inclusive,
        end_exclusive,
        timezone_name=timezone_name,
        required_days=window_days,
    )
    if (
        not isinstance(occupation_lookback_days, int)
        or isinstance(occupation_lookback_days, bool)
        or occupation_lookback_days <= 0
        or occupation_lookback_days > 3650
    ):
        raise TurnoverReportError("occupation_lookback_days must be between 1 and 3650")

    basis = _normalise_basis(inventory_basis)
    status_key = str(inventory_status or "ALL").strip().upper()
    if not status_key or not re.fullmatch(r"[A-Za-z0-9_-]+", status_key):
        raise TurnoverReportError("inventory_status contains unsupported characters")
    owner_sql = _sql_literal(cargo_owner_id)
    start_literal = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_literal = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    definitions = warehouse_template.centers if warehouse_template else OPERATING_CENTER_DEFINITIONS
    center_sql = _union_literals(definitions)
    excluded_sql = _union_codes(OPERATING_CENTER_EXCLUDED_WAREHOUSE_CODES, "wms_code")
    center_days = warehouse_template.center_window_days if warehouse_template else window_days
    if warehouse_template and window_days != warehouse_template.detail_window_days:
        raise TurnoverReportError("template movement window must match the approved detail window")
    center_start = (end_dt - timedelta(days=center_days)).strftime("%Y-%m-%d %H:%M:%S")
    scope_name = "legacy_warehouse_scope" if warehouse_template else "warehouse_scope"
    template_scope = warehouse_template.scope_ctes(cargo_owner_id) + ", " if warehouse_template else ""
    detail_order_sql = "w.detail_order" if warehouse_template else "w.is_center DESC, w.wms_node, w.wms_type, w.wms_code"

    if basis == "available_plus_transit":
        inventory_freshness_expression = "meta.source_table_max_inventory_update_time"
        inventory_source_cte = f"""inventory_source AS (
    SELECT
        TRIM(i.owner) AS cargo_owner_id,
        TRIM(i.wms_code) AS source_wms_code,
        TRIM(i.wms_desc) AS source_warehouse_name,
        TRIM(i.sku_code) AS sku_code,
        SUM(COALESCE(i.available_quantity, 0) + COALESCE(i.in_transit_quantity, 0)) AS inventory_ea,
        COUNT(*) AS inventory_row_count,
        SUM(CASE WHEN COALESCE(i.available_quantity, 0) + COALESCE(i.in_transit_quantity, 0) < 0 THEN 1 ELSE 0 END) AS negative_inventory_count,
        CASE WHEN COUNT(*) > 1 THEN 1 ELSE 0 END AS duplicate_inventory_key_count,
        CAST(NULL AS DATETIME) AS source_max_stock_change_time
    FROM inventory_current AS i
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(i.sku_code)
    WHERE TRIM(i.owner) = {owner_sql}
    GROUP BY TRIM(i.owner), TRIM(i.wms_code), TRIM(i.wms_desc), TRIM(i.sku_code)
), inventory_by_warehouse AS (
    SELECT
        w.cargo_owner_id,
        w.wms_code,
        COALESCE(SUM(i.inventory_ea), 0) AS available_ea,
        COALESCE(SUM(i.inventory_row_count), 0) AS inventory_row_count,
        COALESCE(SUM(i.negative_inventory_count), 0) AS negative_stock_count,
        COALESCE(SUM(i.duplicate_inventory_key_count), 0) AS duplicate_inventory_key_count,
        MAX(i.source_max_stock_change_time) AS source_max_stock_change_time
    FROM warehouse_scope AS w
    LEFT JOIN inventory_source AS i
      ON i.cargo_owner_id = w.cargo_owner_id
      AND i.source_wms_code = w.wms_code
    GROUP BY w.cargo_owner_id, w.wms_code
)"""
    else:
        inventory_freshness_expression = "meta.source_table_max_stock_ingestion_update_time"
        if status_key == "ALL":
            status_filter = ""
        elif status_key in {"ZP_OR_EMPTY", "ZP_OR_NULL", "ZP_OR_BLANK"}:
            status_filter = "AND COALESCE(TRIM(s.inventory_status), '') IN ('', 'ZP')"
        else:
            status_filter = f"AND COALESCE(TRIM(s.inventory_status), '') = {_sql_literal(status_key)}"

        raw_inventory_value = (
            "COALESCE(s.actual_quantity, 0)"
            if basis == "actual_quantity"
            else "COALESCE(s.in_stock_quantity, 0) - COALESCE(s.allot_quantity, 0) "
            "- COALESCE(s.lock_quantity, 0) - COALESCE(s.freeze_quantity, 0) "
            "+ COALESCE(s.move_in_quantity, 0) - COALESCE(s.move_out_quantity, 0)"
        )
        if basis == "record_deductions":
            occupation_cte = f""", occupation_by_sku AS (
    SELECT
        TRIM(o.cargo_owner_id) AS cargo_owner_id,
        TRIM(o.wms_code) AS wms_code,
        TRIM(o.sku_code) AS sku_code,
        SUM(COALESCE(o.lock_quantity, 0) + COALESCE(o.freeze_quantity, 0)) AS occupation_qty
    FROM ywzt_ods.scm_warehous_occupy AS o
    JOIN warehouse_scope AS w
      ON w.cargo_owner_id = TRIM(o.cargo_owner_id)
     AND w.wms_code = TRIM(o.wms_code)
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(o.sku_code)
    WHERE TRIM(o.cargo_owner_id) = {owner_sql}
      AND (o.status <> '1' OR o.status IS NULL)
      AND o.created_date >= DATE_SUB('{end_literal}', INTERVAL {occupation_lookback_days} DAY)
      AND o.created_date < '{end_literal}'
    GROUP BY TRIM(o.cargo_owner_id), TRIM(o.wms_code), TRIM(o.sku_code)
)"""
            effective_inventory = "r.raw_inventory_ea - COALESCE(o.occupation_qty, 0)"
            negative_inventory = "CASE WHEN r.raw_inventory_ea - COALESCE(o.occupation_qty, 0) < 0 THEN 1 ELSE 0 END"
            occupation_join = "LEFT JOIN occupation_by_sku AS o ON o.cargo_owner_id = r.cargo_owner_id AND o.wms_code = r.source_wms_code AND o.sku_code = r.sku_code"
        else:
            occupation_cte = ""
            effective_inventory = "r.raw_inventory_ea"
            negative_inventory = "r.raw_negative_inventory_count"
            occupation_join = ""
        inventory_source_cte = f"""inventory_raw AS (
    SELECT
        TRIM(s.cargo_owner_id) AS cargo_owner_id,
        TRIM(s.wms_code) AS source_wms_code,
        CAST(NULL AS VARCHAR) AS source_warehouse_name,
        TRIM(s.sku_code) AS sku_code,
        SUM({raw_inventory_value}) AS raw_inventory_ea,
        SUM(CASE WHEN ({raw_inventory_value}) < 0 THEN 1 ELSE 0 END) AS raw_negative_inventory_count,
        COUNT(*) AS inventory_row_count,
        MAX(s.stock_change_time) AS source_max_stock_change_time
    FROM ywzt_ods.scm_warehous_manage AS s
    JOIN warehouse_scope AS w
      ON w.cargo_owner_id = TRIM(s.cargo_owner_id)
     AND w.wms_code = TRIM(s.wms_code)
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(s.sku_code)
    WHERE TRIM(s.cargo_owner_id) = {owner_sql}
      AND COALESCE(CAST(s.status AS STRING), '0') = '0'
      {status_filter}
    GROUP BY TRIM(s.cargo_owner_id), TRIM(s.wms_code), TRIM(s.sku_code)
){occupation_cte}, inventory_source AS (
    SELECT
        r.cargo_owner_id,
        r.source_wms_code,
        r.source_warehouse_name,
        r.sku_code,
        {effective_inventory} AS inventory_ea,
        r.inventory_row_count,
        {negative_inventory} AS negative_inventory_count,
        0 AS duplicate_inventory_key_count,
        r.source_max_stock_change_time
    FROM inventory_raw AS r
    {occupation_join}
), inventory_by_warehouse AS (
    SELECT
        w.cargo_owner_id,
        w.wms_code,
        COALESCE(SUM(i.inventory_ea), 0) AS available_ea,
        COALESCE(SUM(i.inventory_row_count), 0) AS inventory_row_count,
        COALESCE(SUM(i.negative_inventory_count), 0) AS negative_stock_count,
        COALESCE(SUM(i.duplicate_inventory_key_count), 0) AS duplicate_inventory_key_count,
        MAX(i.source_max_stock_change_time) AS source_max_stock_change_time
    FROM warehouse_scope AS w
    LEFT JOIN inventory_source AS i
      ON i.cargo_owner_id = w.cargo_owner_id
     AND i.source_wms_code = w.wms_code
    GROUP BY w.cargo_owner_id, w.wms_code
)"""

    sql = f"""-- Reviewed profile: HJXH {len(definitions)} operating centers; quantities are EA.
-- Window: [{start_literal}, {end_literal}) {timezone_name}; physical rows feed the governed center rollup.
WITH approved_centers AS (
    {center_sql}
), profile_exclusions AS (
    {excluded_sql}
), wms_raw AS (
    SELECT
        TRIM(m.wms_code) AS wms_code,
        TRIM(COALESCE(m.wms_desc, '')) AS warehouse_name,
        TRIM(m.wms_node) AS wms_node,
        TRIM(m.wms_type) AS wms_type,
        NULLIF(TRIM(COALESCE(m.belonging_center, '')), '') AS explicit_parent_code,
        NULLIF(TRIM(COALESCE(m.senior_wms, '')), '') AS fallback_parent_code,
        m.updated_date,
        m.id,
        CASE
            WHEN TRIM(m.wms_node) = '10' THEN TRIM(m.wms_code)
            WHEN NULLIF(TRIM(COALESCE(m.belonging_center, '')), '') IS NOT NULL
                THEN TRIM(m.belonging_center)
            ELSE NULLIF(TRIM(COALESCE(m.senior_wms, '')), '')
        END AS candidate_center_code,
        ROW_NUMBER() OVER (
            PARTITION BY TRIM(m.cargo_owner_id), TRIM(m.wms_code)
            ORDER BY m.updated_date DESC, m.id DESC
        ) AS warehouse_rank
    FROM ywzt_ods.master_wms AS m
    WHERE TRIM(m.cargo_owner_id) = {owner_sql}
      AND COALESCE(CAST(m.status AS STRING), '0') = '0'
      AND COALESCE(CAST(m.wms_status AS STRING), '10') = '10'
      AND TRIM(m.wms_node) IN ('10', '12', '13', '14')
      AND TRIM(m.wms_type) IN ('10', '11', '14')
      AND COALESCE(m.wms_desc, '') NOT LIKE '%残次%'
      AND COALESCE(m.wms_desc, '') NOT LIKE '%次品%'
      AND COALESCE(m.wms_desc, '') NOT LIKE '%废品%'
      AND COALESCE(m.wms_desc, '') NOT LIKE '%报废%'
      AND COALESCE(m.wms_desc, '') NOT LIKE '%样品%'
      AND COALESCE(m.wms_desc, '') NOT LIKE '%包材%'
      AND COALESCE(m.wms_desc, '') NOT LIKE '%工厂%'
      AND COALESCE(m.wms_desc, '') NOT LIKE '%门店%'
), wms_variants AS (
    SELECT
        wms_code,
        COUNT(DISTINCT CONCAT_WS('|', warehouse_name, wms_node, wms_type,
            COALESCE(explicit_parent_code, ''), COALESCE(fallback_parent_code, ''))) AS mapping_variant_count
    FROM wms_raw
    GROUP BY wms_code
), wms_latest AS (
    SELECT *
    FROM wms_raw
    WHERE warehouse_rank = 1
), center_master AS (
    SELECT
        c.center_code,
        c.expected_name,
        c.center_order,
        w.wms_code,
        w.warehouse_name,
        w.wms_node,
        w.wms_type,
        w.updated_date,
        w.explicit_parent_code,
        w.fallback_parent_code,
        COALESCE(v.mapping_variant_count, 0) AS mapping_variant_count,
        CASE
            WHEN w.wms_code IS NOT NULL
             AND (COALESCE(w.explicit_parent_code, '') <> w.wms_code
               OR COALESCE(w.fallback_parent_code, '') <> w.wms_code)
                THEN 1
            ELSE 0
         END AS center_parent_anomaly,
         CASE
             WHEN w.wms_code IS NOT NULL
              AND TRIM(COALESCE(w.warehouse_name, '')) <> TRIM(COALESCE(c.expected_name, ''))
                 THEN 1
             ELSE 0
         END AS center_name_mismatch
    FROM approved_centers AS c
    LEFT JOIN wms_latest AS w
      ON w.wms_code = c.center_code
     AND w.wms_node = '10'
    LEFT JOIN wms_variants AS v
      ON v.wms_code = c.center_code
), detail_candidates AS (
    SELECT
        w.wms_code,
        w.warehouse_name,
        w.wms_node,
        w.wms_type,
        w.updated_date,
        c.center_code,
        c.expected_name,
        c.center_order,
        COALESCE(v.mapping_variant_count, 0) AS mapping_variant_count
    FROM wms_latest AS w
    LEFT JOIN wms_variants AS v
      ON v.wms_code = w.wms_code
    JOIN center_master AS c
      ON w.candidate_center_code = c.center_code
    WHERE w.wms_node IN ('12', '13', '14')
      AND w.wms_code NOT IN (SELECT wms_code FROM profile_exclusions)
), {scope_name} AS (
    SELECT
        {owner_sql} AS cargo_owner_id,
        c.wms_code,
        c.warehouse_name,
        c.wms_node,
        c.wms_type,
        c.center_code AS belonging_center_code,
        c.warehouse_name AS belonging_center,
        c.expected_name,
        c.center_order,
        1 AS is_center,
        c.mapping_variant_count,
         c.center_parent_anomaly,
         c.center_name_mismatch,
         c.updated_date
    FROM center_master AS c
    WHERE c.wms_code IS NOT NULL
    UNION ALL
    SELECT
        {owner_sql} AS cargo_owner_id,
        d.wms_code,
        d.warehouse_name,
        d.wms_node,
        d.wms_type,
        d.center_code AS belonging_center_code,
        c.warehouse_name AS belonging_center,
        d.expected_name,
        d.center_order,
        0 AS is_center,
        d.mapping_variant_count,
         0 AS center_parent_anomaly,
         0 AS center_name_mismatch,
         d.updated_date
    FROM detail_candidates AS d
    JOIN center_master AS c
      ON c.center_code = d.center_code
), {template_scope}sku_scope AS (
    SELECT DISTINCT TRIM(sku_code) AS sku_code
    FROM ywzt_ods.master_sku
    WHERE TRIM(sku_group) = '50'
      AND TRIM(sku_code) <> ''
), source_table_meta AS (
    SELECT
        MIN(CASE WHEN table_name IN ('scm_warehous_manage', 'scm_warehous_occupy', 'scm_po_header', 'scm_po_detail', 'scm_transfer_order_header', 'scm_transfer_order_detail', 'scm_sales_shipment_allocate_wms', 'scm_stock_history') THEN update_time END) AS source_table_max_inventory_update_time,
        MAX(CASE WHEN table_name = 'scm_warehous_manage' THEN update_time END) AS source_table_max_stock_ingestion_update_time,
        MAX(CASE WHEN table_name = 'master_wms' THEN update_time END) AS source_table_max_wms_ingestion_update_time,
        MAX(CASE WHEN table_name = 'master_sku' THEN update_time END) AS source_table_max_sku_ingestion_update_time,
        CASE WHEN COUNT(CASE WHEN table_name IN ('scm_sales_shipment_header','scm_sales_shipment_detail','scm_sales_order_header','scm_sales_order_detail','master_store') AND update_time IS NOT NULL THEN 1 END) = 5
          THEN MIN(CASE WHEN table_name IN ('scm_sales_shipment_header','scm_sales_shipment_detail','scm_sales_order_header','scm_sales_order_detail','master_store') THEN update_time END)
          ELSE NULL END AS source_table_max_outbound_ingestion_update_time
    FROM information_schema.tables
    WHERE table_schema = 'ywzt_ods'
      AND table_name IN ('scm_warehous_manage', 'scm_warehous_occupy', 'scm_po_header', 'scm_po_detail', 'scm_transfer_order_header', 'scm_transfer_order_detail', 'scm_sales_shipment_allocate_wms', 'scm_stock_history', 'master_wms', 'master_sku', 'scm_sales_shipment_header','scm_sales_shipment_detail','scm_sales_order_header','scm_sales_order_detail','master_store')
), {inventory_source_cte}, outbound_grouped AS (
    SELECT document_code AS outbound_code, cargo_owner AS cargo_owner_id,
           wms_code, MAX(business_at) AS source_max_ship_at,
           COUNT(DISTINCT wms_code) AS facility_variant_count,
           COUNT(DISTINCT business_at) AS ship_time_variant_count
    FROM sales_current
    GROUP BY source_kind, document_code, cargo_owner, wms_code
), outbound_by_warehouse AS (
    SELECT e.cargo_owner AS cargo_owner_id, e.wms_code,
           COALESCE(SUM(e.quantity), 0) AS outbound_7d_ea,
           COALESCE(SUM(CASE WHEN e.business_at >= '{center_start}' THEN e.quantity ELSE 0 END), 0) AS center_outbound_ea,
           SUM(CASE WHEN e.business_at >= '{center_start}' THEN 1 ELSE 0 END) AS center_outbound_row_count,
           COUNT(*) AS outbound_row_count,
           SUM(CASE WHEN e.quantity < 0 OR e.quantity IS NULL THEN 1 ELSE 0 END) AS negative_outbound_count,
           MAX(e.business_at) AS source_max_ship_at
    FROM sales_current e
    JOIN warehouse_scope w ON w.cargo_owner_id=e.cargo_owner AND w.wms_code=e.wms_code
    JOIN sku_scope ss ON ss.sku_code=e.sku_code
    GROUP BY e.cargo_owner, e.wms_code
), inventory_control AS (
    SELECT
        COALESCE(SUM(i.available_ea), 0) AS control_inventory_total_ea,
        COALESCE(SUM(i.inventory_row_count), 0) AS control_inventory_row_count,
        COALESCE(SUM(i.negative_stock_count), 0) AS control_negative_stock_count,
        COALESCE(SUM(i.duplicate_inventory_key_count), 0) AS control_duplicate_inventory_key_count
    FROM inventory_by_warehouse AS i
), outbound_control AS (
    SELECT
        COALESCE(SUM(o.outbound_7d_ea), 0) AS control_outbound_total_ea,
        COALESCE(SUM(o.center_outbound_ea), 0) AS control_center_outbound_total_ea,
        COALESCE(SUM(o.center_outbound_row_count), 0) AS control_center_outbound_row_count,
        COALESCE(SUM(o.outbound_row_count), 0) AS control_outbound_row_count,
        COALESCE(SUM(o.negative_outbound_count), 0) AS control_negative_outbound_count
    FROM outbound_by_warehouse AS o
), warehouse_quality AS (
    SELECT
        COUNT(*) AS warehouse_scope_row_count,
        COALESCE(SUM(is_center), 0) AS warehouse_center_row_count,
        COALESCE(SUM(CASE WHEN is_center = 0 THEN 1 ELSE 0 END), 0) AS warehouse_detail_row_count,
        COALESCE(SUM(CASE WHEN mapping_variant_count > 1 THEN 1 ELSE 0 END), 0) AS warehouse_mapping_conflict_count,
         COALESCE(SUM(center_name_mismatch), 0) AS center_name_mismatch_count,
        COALESCE(SUM(center_parent_anomaly), 0) AS center_parent_anomaly_count,
        MAX(updated_date) AS source_table_max_wms_updated_date
    FROM warehouse_scope
), inventory_quality AS (
    SELECT
        COALESCE(SUM(CASE WHEN i.inventory_row_count = 0 THEN 1 ELSE 0 END), 0) AS inventory_missing_warehouse_count,
        COALESCE(SUM(i.inventory_row_count), 0) AS source_table_inventory_row_count
    FROM inventory_by_warehouse AS i
), outbound_quality AS (
    SELECT
        COUNT(*) AS source_table_outbound_header_count,
        COALESCE(SUM(CASE WHEN facility_variant_count > 1 OR ship_time_variant_count > 1 THEN 1 ELSE 0 END), 0) AS ambiguous_outbound_count,
        MAX(source_max_ship_at) AS source_table_max_ship_at
    FROM outbound_grouped
), inventory_name_quality AS (
    SELECT
        COALESCE(SUM(CASE WHEN code_count > 1 THEN 1 ELSE 0 END), 0) AS inventory_name_conflict_count
    FROM (
        SELECT warehouse_name, COUNT(DISTINCT wms_code) AS code_count
        FROM warehouse_scope
        WHERE warehouse_name <> ''
        GROUP BY warehouse_name
    ) AS names
)
SELECT
    w.cargo_owner_id,
    w.wms_code,
    w.warehouse_name,
    w.wms_type,
    w.wms_node,
    w.belonging_center_code,
    w.belonging_center,
    w.center_order,
    COALESCE(i.available_ea, 0) AS available_ea,
    COALESCE(o.outbound_7d_ea, 0) AS outbound_7d_ea,
    COALESCE(o.center_outbound_ea, 0) AS center_outbound_ea,
    COALESCE(o.center_outbound_row_count, 0) AS center_outbound_row_count,
    COALESCE(i.inventory_row_count, 0) AS inventory_row_count,
    COALESCE(i.negative_stock_count, 0) AS negative_stock_count,
    COALESCE(i.duplicate_inventory_key_count, 0) AS duplicate_inventory_key_count,
    COALESCE(o.outbound_row_count, 0) AS outbound_row_count,
    COALESCE(o.negative_outbound_count, 0) AS negative_outbound_count,
    i.source_max_stock_change_time,
    o.source_max_ship_at,
    {inventory_freshness_expression} AS source_max_inventory_update_time,
    meta.source_table_max_inventory_update_time,
    meta.source_table_max_stock_ingestion_update_time,
    meta.source_table_max_wms_ingestion_update_time,
    meta.source_table_max_sku_ingestion_update_time,
    meta.source_table_max_outbound_ingestion_update_time,
    oq.source_table_max_ship_at,
    wq.source_table_max_wms_updated_date,
    wq.warehouse_scope_row_count,
    wq.warehouse_center_row_count,
    wq.warehouse_detail_row_count,
    wq.warehouse_mapping_conflict_count,
    0 AS invalid_parent_mapping_count,
    0 AS unresolved_mapping_count,
    wq.center_parent_anomaly_count,
    wq.center_name_mismatch_count,
    iq.inventory_missing_warehouse_count,
    nq.inventory_name_conflict_count,
    iq.source_table_inventory_row_count,
    (SELECT COUNT(*) FROM ywzt_ods.scm_warehous_manage) AS inventory_base_source_row_count,
    oq.source_table_outbound_header_count,
    oq.ambiguous_outbound_count,
    ic.control_inventory_total_ea,
    oc.control_outbound_total_ea,
    oc.control_center_outbound_total_ea,
    oc.control_center_outbound_row_count,
    ic.control_inventory_row_count,
    oc.control_outbound_row_count,
    ic.control_negative_stock_count,
    ic.control_duplicate_inventory_key_count,
    oc.control_negative_outbound_count,
    {len(definitions)} AS expected_center_count
FROM warehouse_scope AS w
LEFT JOIN inventory_by_warehouse AS i
  ON i.cargo_owner_id = w.cargo_owner_id
 AND i.wms_code = w.wms_code
LEFT JOIN outbound_by_warehouse AS o
  ON o.cargo_owner_id = w.cargo_owner_id
 AND o.wms_code = w.wms_code
CROSS JOIN source_table_meta AS meta
CROSS JOIN warehouse_quality AS wq
CROSS JOIN inventory_quality AS iq
CROSS JOIN outbound_quality AS oq
CROSS JOIN inventory_name_quality AS nq
CROSS JOIN inventory_control AS ic
CROSS JOIN outbound_control AS oc
ORDER BY w.center_order, {detail_order_sql}"""
    sql = bind_business_sales(sql, start_dt, end_dt,
                              scope=QueryScope(owner=cargo_owner_id, finished_goods=True,
                                  warehouse_codes=tuple(w.code for w in warehouse_template.warehouses) if warehouse_template else ()))
    return bind_current_inventory(sql) if basis == "available_plus_transit" else sql


__all__ = [
    "OPERATING_CENTER_DEFINITIONS",
    "OPERATING_CENTER_EXCLUDED_WAREHOUSE_CODES",
    "OPERATING_CENTER_ALLOWED_WMS_TYPES",
    "build_operating_center_turnover_sql",
]
