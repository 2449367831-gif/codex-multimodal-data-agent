/* 近 60 天生产库存
   粒度：仓库 + SKU
   表：scm_warehous_manage_3d
   说明：
   - 仅自有仓库存包含 production_date。
   - 取 scm_warehous_manage_3d 最近有数据的 created_date 当库存快照日。
   - 生产日期按当前日期往前推 60 天，不按自然月。
*/
WITH latest_snapshot AS (
    SELECT MAX(DATE(created_date)) AS snapshot_date
    FROM scm_warehous_manage_3d
    WHERE cargo_owner_id = 'HJXH'
)
SELECT
    wm.cargo_owner_id,
    wm.wms_code AS warehouse_code,
    w.wms_desc AS warehouse_name,
    wm.sku_code,
    wm.sku_name,
    SUM(wm.in_stock_quantity) AS recent_stock_qty,
    MAX(ls.snapshot_date) AS stock_snapshot_date
FROM scm_warehous_manage_3d wm
JOIN latest_snapshot ls
    ON wm.created_date >= ls.snapshot_date
   AND wm.created_date < ls.snapshot_date + INTERVAL 1 DAY
LEFT JOIN master_wms w
    ON w.wms_code = wm.wms_code
LEFT JOIN master_sku m
    ON m.sku_code = wm.sku_code
WHERE wm.production_date > CURDATE() - INTERVAL 60 DAY
  AND wm.cargo_owner_id = 'HJXH'
  AND m.sku_group = '50'
GROUP BY
    wm.cargo_owner_id,
    wm.wms_code,
    w.wms_desc,
    wm.sku_code,
    wm.sku_name;
