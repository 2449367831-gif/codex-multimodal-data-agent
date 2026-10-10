/* 三方仓近 60 天生产库存
   粒度：仓库 + SKU
   说明：
   - 三方仓近 60 天库存优先使用本 SQL。
   - 若本 SQL 按仓库 + SKU 没有数据，再使用 realtime_stock.sql 的对应仓库 + SKU 数据。
   - 取 scm_warehous_manage_3d 最近有数据的 created_date 当库存快照日。
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
