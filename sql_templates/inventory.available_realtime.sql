-- Contract: metrics/inventory.available_realtime.yaml
WITH stock AS (
  SELECT
    cargo_owner_id,
    TRIM(wms_code) AS wms_code,
    sku_code,
    SUM(IFNULL(in_stock_quantity, 0) - IFNULL(allot_quantity, 0) - IFNULL(lock_quantity, 0)
      - IFNULL(freeze_quantity, 0) + IFNULL(move_in_quantity, 0)) AS stock_after_record_deductions,
    MAX(stock_change_time) AS source_max_stock_change_time
  FROM ywzt_ods.scm_warehous_manage
  WHERE status = '0'
  GROUP BY cargo_owner_id, TRIM(wms_code), sku_code
), occupation AS (
  SELECT
    cargo_owner_id,
    TRIM(wms_code) AS wms_code,
    sku_code,
    SUM(IFNULL(lock_quantity, 0) + IFNULL(freeze_quantity, 0)) AS occupation_qty
  FROM ywzt_ods.scm_warehous_occupy
  WHERE (status <> '1' OR status IS NULL)
    AND created_date >= DATE_SUB(NOW(), INTERVAL :occupation_lookback_days DAY)
  GROUP BY cargo_owner_id, TRIM(wms_code), sku_code
)
SELECT
  s.cargo_owner_id,
  s.wms_code,
  s.sku_code,
  GREATEST(s.stock_after_record_deductions - IFNULL(o.occupation_qty, 0), 0) AS available_ea,
  s.source_max_stock_change_time
FROM stock AS s
LEFT JOIN occupation AS o
  ON o.cargo_owner_id = s.cargo_owner_id
 AND o.wms_code = s.wms_code
 AND o.sku_code = s.sku_code;

