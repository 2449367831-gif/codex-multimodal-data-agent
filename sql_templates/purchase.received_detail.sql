-- Candidate receipt-detail contract. The caller must resolve purchase_basis.
WITH header_variants AS (
  SELECT DISTINCT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(warehous_code) AS inbound_code,
         COALESCE(TRIM(po_order_code), '') AS purchase_order_code,
         COALESCE(TRIM(CAST(warehous_type AS STRING)), '') AS inbound_type,
         actual_arrival_date,
         COALESCE(TRIM(CAST(status AS STRING)), '') AS logical_status,
         LOWER(COALESCE(TRIM(CAST(_fdl_marked_deleted AS STRING)), 'false')) AS deleted
  FROM ywzt_ods.scm_warehous_header
), header_controls AS (
  SELECT cargo_owner, inbound_code, COUNT(*) AS header_variants
  FROM header_variants GROUP BY cargo_owner, inbound_code
), eligible_headers AS (
  SELECT cargo_owner, inbound_code, MIN(actual_arrival_date) AS actual_arrival_date
  FROM header_variants
  WHERE logical_status = '0' AND deleted = 'false'
    AND actual_arrival_date < :arrival_before
    AND (
      (:purchase_basis = 'purchase_order_reference' AND purchase_order_code <> '')
      OR (:purchase_basis = 'purchase_type' AND inbound_type = '10')
      OR (:purchase_basis = 'both' AND purchase_order_code <> '' AND inbound_type = '10')
    )
  GROUP BY cargo_owner, inbound_code
), detail_source AS (
  SELECT TRIM(CAST(id AS STRING)) AS detail_id,
         TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(warehous_code) AS inbound_code,
         TRIM(item_code) AS item_code,
         TRIM(wms_code) AS warehouse_code, TRIM(sku_code) AS sku_code,
         TRIM(batch_number) AS batch_number, production_date,
         received_quantity AS quantity, TRIM(CAST(unit AS STRING)) AS quantity_unit,
         COALESCE(TRIM(CAST(status AS STRING)), '') AS logical_status,
         LOWER(COALESCE(TRIM(CAST(_fdl_marked_deleted AS STRING)), 'false')) AS deleted,
         COUNT(*) OVER (
           PARTITION BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(warehous_code), TRIM(CAST(id AS STRING))
         ) AS detail_variants
  FROM ywzt_ods.scm_warehous_detail
), warehouse_names AS (
  SELECT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS warehouse_code,
         MIN(NULLIF(TRIM(wms_desc), '')) AS warehouse_name,
         COUNT(DISTINCT NULLIF(TRIM(wms_desc), '')) AS warehouse_variants
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
  GROUP BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(wms_code)
), receipt_facts AS (
  SELECT d.cargo_owner, d.inbound_code, d.detail_id, d.item_code,
         d.warehouse_code, w.warehouse_name, d.sku_code, d.batch_number,
         d.production_date, d.quantity, d.quantity_unit, h.actual_arrival_date,
         hc.header_variants, d.detail_variants,
         COALESCE(w.warehouse_variants, 0) AS warehouse_variants
  FROM detail_source d
  JOIN eligible_headers h
    ON h.cargo_owner = d.cargo_owner AND h.inbound_code = d.inbound_code
  JOIN header_controls hc
    ON hc.cargo_owner = d.cargo_owner AND hc.inbound_code = d.inbound_code
  LEFT JOIN warehouse_names w
    ON w.cargo_owner = d.cargo_owner AND w.warehouse_code = d.warehouse_code
  WHERE d.logical_status = '0' AND d.deleted = 'false'
    AND d.production_date >= :production_start
    AND d.production_date < :production_end
)
SELECT *, COUNT(*) OVER () AS matched_count,
       SUM(quantity) OVER (PARTITION BY quantity_unit) AS unit_quantity_total
FROM receipt_facts
ORDER BY cargo_owner, inbound_code, detail_id
LIMIT :probe_limit
