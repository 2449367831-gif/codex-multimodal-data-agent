-- Contract: metrics/inventory.finished_goods_realtime.yaml
WITH finished_goods_skus AS (
  SELECT DISTINCT TRIM(sku_code) AS sku_code
  FROM ywzt_ods.master_sku
  WHERE TRIM(sku_group) = '50'
), stock AS (
  SELECT
    TRIM(s.owner) AS cargo_owner_id,
    TRIM(s.wms_desc) AS wms_code,
    TRIM(s.sku_code) AS sku_code,
    SUM(IFNULL(s.available_quantity, 0) + IFNULL(s.in_transit_quantity, 0)) AS realtime_finished_goods_ea,
    CAST(NULL AS DATETIME) AS source_max_stock_change_time
  FROM ywzt_ods.sku_inventory AS s
  JOIN finished_goods_skus AS fg
    ON fg.sku_code = TRIM(s.sku_code)
  GROUP BY TRIM(s.owner), TRIM(s.wms_desc), TRIM(s.sku_code)
)
SELECT
  cargo_owner_id,
  wms_code,
  sku_code,
  realtime_finished_goods_ea,
  source_max_stock_change_time
FROM stock;
