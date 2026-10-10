-- Contract: metrics/sales_order.so07_detail.yaml
WITH requested_order_keys AS (
  SELECT DISTINCT TRIM(CAST(company_code AS STRING)) AS cargo_owner,
         TRIM(sales_order_code) AS sales_order_code
  FROM ywzt_ods.scm_sales_order_header
  WHERE (deleted_flag IS NULL OR deleted_flag <> 1)
    AND (_fdl_marked_deleted IS NULL OR LOWER(TRIM(_fdl_marked_deleted)) <> 'true')
    AND TRIM(order_type) = 'SO07'
    AND ((:start_inclusive IS NULL AND :end_exclusive IS NULL) OR created IS NULL OR (
      created >= :start_inclusive AND created < :end_exclusive))
), header_variants AS (
  SELECT DISTINCT TRIM(CAST(company_code AS STRING)) AS cargo_owner,
         TRIM(sales_order_code) AS sales_order_code,
         TRIM(facility_code) AS warehouse_code,
         TRIM(store_code) AS customer_code,
         NULLIF(TRIM(store_name), '') AS customer_name,
         TRIM(order_type) AS order_type,
         created AS order_created_at,
         updated AS header_updated_at,
         TRIM(CAST(status AS STRING)) AS order_status,
         TRIM(CAST(deleted_flag AS STRING)) AS deleted_flag,
         LOWER(TRIM(CAST(_fdl_marked_deleted AS STRING))) AS deleted
  FROM ywzt_ods.scm_sales_order_header source_header
  WHERE (deleted_flag IS NULL OR deleted_flag <> 1)
    AND (_fdl_marked_deleted IS NULL OR LOWER(TRIM(_fdl_marked_deleted)) <> 'true')
    AND EXISTS (
      SELECT 1 FROM requested_order_keys requested
      WHERE requested.cargo_owner <=> TRIM(CAST(source_header.company_code AS STRING))
        AND requested.sales_order_code <=> TRIM(source_header.sales_order_code)
    )
), order_controls AS (
  SELECT *, COUNT(*) OVER (PARTITION BY cargo_owner, sales_order_code) AS order_variants
  FROM header_variants
), eligible_headers AS (
  SELECT h.cargo_owner, h.sales_order_code, h.warehouse_code, h.customer_code,
         h.customer_name, h.order_type, h.order_created_at, h.header_updated_at, h.order_status,
         h.order_variants,
         CASE WHEN h.deleted_flag = '0' AND h.deleted = 'false' THEN 0 ELSE 1 END AS source_quality_errors
  FROM order_controls h
  WHERE h.order_type = 'SO07'
    AND ((:start_inclusive IS NULL AND :end_exclusive IS NULL) OR h.order_created_at IS NULL OR (
      h.order_created_at >= :start_inclusive
      AND h.order_created_at < :end_exclusive))
), detail_source AS (
  SELECT TRIM(CAST(id AS STRING)) AS detail_id,
         TRIM(CAST(company_code AS STRING)) AS cargo_owner,
         TRIM(sales_order_code) AS sales_order_code,
         TRIM(facility_code) AS warehouse_code,
         TRIM(sku_code) AS sku_code,
         NULLIF(TRIM(sku_name), '') AS sku_name,
         request_qty AS request_quantity,
         updated AS detail_updated_at,
         NULLIF(TRIM(quantity_u_m), '') AS quantity_unit_reference,
         NULLIF(TRIM(sales_unit), '') AS sales_unit_reference,
         TRIM(CAST(status AS STRING)) AS detail_status,
         LOWER(TRIM(CAST(_fdl_marked_deleted AS STRING))) AS deleted,
         CASE WHEN LOWER(TRIM(_fdl_marked_deleted)) = 'false' THEN 0 ELSE 1 END AS source_quality_errors,
         COUNT(*) OVER (
           PARTITION BY TRIM(CAST(company_code AS STRING)), TRIM(sales_order_code), TRIM(CAST(id AS STRING))
         ) AS detail_variants
  FROM ywzt_ods.scm_sales_order_detail source_detail
  WHERE TRIM(CAST(status AS STRING)) = '0'
    AND (_fdl_marked_deleted IS NULL OR LOWER(TRIM(_fdl_marked_deleted)) <> 'true')
    AND EXISTS (
      SELECT 1 FROM eligible_headers eligible
      WHERE eligible.cargo_owner = TRIM(CAST(source_detail.company_code AS STRING))
        AND eligible.sales_order_code = TRIM(source_detail.sales_order_code)
    )
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
         h.header_updated_at, d.detail_updated_at, d.quantity_unit_reference, d.sales_unit_reference,
         h.order_variants, COALESCE(w.warehouse_variants, 0) AS warehouse_variants,
         h.source_quality_errors + COALESCE(d.source_quality_errors, 0) AS source_quality_errors
  FROM eligible_headers h
  LEFT JOIN detail_source d
    ON d.cargo_owner = h.cargo_owner AND d.sales_order_code = h.sales_order_code
  LEFT JOIN warehouse_names w
    ON w.cargo_owner = h.cargo_owner AND w.warehouse_code = COALESCE(NULLIF(d.warehouse_code, ''), h.warehouse_code)
), scoped_facts AS (
  SELECT * FROM order_facts
), order_keys AS (
  SELECT DISTINCT cargo_owner, sales_order_code, header_updated_at FROM scoped_facts
), totals AS (
  SELECT COUNT(*) AS total_order_count,
         SUM(CASE WHEN header_updated_at IS NULL THEN 1 ELSE 0 END) AS missing_header_updated_count,
         MAX(header_updated_at) AS source_header_latest_updated
  FROM order_keys
)
SELECT f.*, totals.*, COUNT(*) OVER () AS matched_count,
       SUM(CASE WHEN detail_id IS NOT NULL AND detail_updated_at IS NULL THEN 1 ELSE 0 END) OVER () AS missing_detail_updated_count,
       SUM(CASE WHEN detail_id IS NOT NULL AND quantity_unit_reference IS NULL THEN 1 ELSE 0 END) OVER () AS missing_quantity_unit_reference_count,
       SUM(CASE WHEN detail_id IS NOT NULL AND sales_unit_reference IS NULL THEN 1 ELSE 0 END) OVER () AS missing_sales_unit_reference_count,
       MAX(detail_updated_at) OVER () AS source_detail_latest_updated,
       SUM(request_quantity) OVER () AS total_request_quantity
FROM scoped_facts f CROSS JOIN totals
ORDER BY cargo_owner, sales_order_code, detail_id
LIMIT :probe_limit
