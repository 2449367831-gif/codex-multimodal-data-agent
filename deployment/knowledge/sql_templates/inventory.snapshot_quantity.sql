-- Contract: metrics/inventory.snapshot_quantity.yaml
-- Return the contract grain. The execution layer applies optional owner, warehouse, and SKU scope.
SELECT
  backups_date,
  cargo_owner_id,
  TRIM(wms_code) AS wms_code,
  sku_code,
  SUM(IFNULL(in_stock_quantity, 0)) AS snapshot_ea,
  MAX(backups_date) AS source_max_snapshot_date
FROM ywzt_ods.scm_warehous_manage_backups
WHERE backups_date = :snapshot_date
GROUP BY backups_date, cargo_owner_id, TRIM(wms_code), sku_code;
