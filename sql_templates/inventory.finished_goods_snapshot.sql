-- Contract: metrics/inventory.finished_goods_snapshot.yaml
WITH finished_goods_skus AS (
  SELECT DISTINCT TRIM(sku_code) AS sku_code
  FROM ywzt_ods.master_sku
  WHERE TRIM(sku_group) = '50'
)
SELECT
  b.backups_date,
  TRIM(b.cargo_owner_id) AS cargo_owner_id,
  TRIM(b.wms_code) AS wms_code,
  TRIM(b.sku_code) AS sku_code,
  SUM(IFNULL(b.in_stock_quantity, 0)) AS snapshot_finished_goods_ea
FROM ywzt_ods.scm_warehous_manage_backups AS b
JOIN finished_goods_skus AS fg
  ON fg.sku_code = TRIM(b.sku_code)
WHERE b.backups_date = :snapshot_date
GROUP BY b.backups_date, TRIM(b.cargo_owner_id), TRIM(b.wms_code), TRIM(b.sku_code);
