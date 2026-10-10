-- Contract: metrics/sales_order.so07_detail.yaml
WITH header_variants AS (
  SELECT DISTINCT TRIM(CAST(company_code AS STRING)) AS cargo_owner,
         TRIM(sales_order_code) AS sales_order_code,
         TRIM(facility_code) AS warehouse_code,
         TRIM(store_code) AS customer_code,
         NULLIF(TRIM(store_name), '') AS customer_name,
         TRIM(order_type) AS order_type,
         created AS order_created_at,
         TRIM(CAST(status AS STRING)) AS order_status,
         COALESCE(TRIM(CAST(deleted_flag AS STRING)), '0') AS deleted_flag,
         LOWER(COALESCE(TRIM(CAST(_fdl_marked_deleted AS STRING)), 'false')) AS deleted
  FROM ywzt_ods.scm_sales_order_header
), order_controls AS (
  SELECT cargo_owner, sales_order_code, COUNT(*) AS order_variants
  FROM header_variants GROUP BY cargo_owner, sales_order_code
), eligible_headers AS (
  SELECT h.cargo_owner, h.sales_order_code, h.warehouse_code, h.customer_code,
         h.customer_name, h.order_type, h.order_created_at, h.order_status,
         c.order_variants
  FROM header_variants h
  JOIN order_controls c
    ON c.cargo_owner = h.cargo_owner AND c.sales_order_code = h.sales_order_code
  WHERE h.order_type = 'SO07'
    AND h.deleted_flag = '0' AND h.deleted = 'false'
    AND h.order_created_at >= :start_inclusive
    AND h.order_created_at < :end_exclusive
), detail_source AS (
  SELECT TRIM(CAST(id AS STRING)) AS detail_id,
         TRIM(CAST(company_code AS STRING)) AS cargo_owner,
         TRIM(sales_order_code) AS sales_order_code,
         TRIM(facility_code) AS warehouse_code,
         TRIM(sku_code) AS sku_code,
         NULLIF(TRIM(sku_name), '') AS sku_name,
         request_qty AS request_quantity,
         TRIM(CAST(status AS STRING)) AS detail_status,
         LOWER(COALESCE(TRIM(CAST(_fdl_marked_deleted AS STRING)), 'false')) AS deleted,
         COUNT(*) OVER (
           PARTITION BY TRIM(CAST(company_code AS STRING)), TRIM(sales_order_code), TRIM(CAST(id AS STRING))
         ) AS detail_variants
  FROM ywzt_ods.scm_sales_order_detail
), warehouse_names AS (
  SELECT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS warehouse_code,
         MIN(NULLIF(TRIM(wms_desc), '')) AS warehouse_name,
         COUNT(DISTINCT NULLIF(TRIM(wms_desc), '')) AS warehouse_variants
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
  GROUP BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(wms_code)
), order_facts AS (
  SELECT h.cargo_owner, h.sales_order_code, h.order_created_at,
         h.customer_code, h.customer_name, COALESCE(NULLIF(d.warehouse_code, ''), h.warehouse_code) AS warehouse_code,
         w.warehouse_name, h.order_type, h.order_status, d.detail_id,
         d.sku_code, d.sku_name, d.request_quantity, d.detail_variants,
         h.order_variants, COALESCE(w.warehouse_variants, 0) AS warehouse_variants
  FROM eligible_headers h
  LEFT JOIN detail_source d
    ON d.cargo_owner = h.cargo_owner AND d.sales_order_code = h.sales_order_code
   AND d.detail_status = '0' AND d.deleted = 'false'
  LEFT JOIN warehouse_names w
    ON w.cargo_owner = h.cargo_owner AND w.warehouse_code = COALESCE(NULLIF(d.warehouse_code, ''), h.warehouse_code)
), scoped_facts AS (
  SELECT * FROM order_facts
), order_keys AS (
  SELECT DISTINCT cargo_owner, sales_order_code FROM scoped_facts
), totals AS (
  SELECT COUNT(*) AS total_order_count FROM order_keys
)
SELECT f.*, totals.total_order_count, COUNT(*) OVER () AS matched_count,
       SUM(request_quantity) OVER () AS total_request_quantity
FROM scoped_facts f CROSS JOIN totals
ORDER BY cargo_owner, sales_order_code, detail_id
LIMIT :probe_limit
