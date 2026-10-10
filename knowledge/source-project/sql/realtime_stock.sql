/* 实时库存：提货表整合用简化版
   粒度：仓库 + SKU
   说明：
   - stock_qty 使用可用数量口径，扣减分配、锁定/冻结后不小于 0。
   - recent_stock_qty 为生产日期近 60 天内的在库数量。
*/
WITH inventory AS (
    SELECT
        w.wms_code,
        w.cargo_owner_id,
        w.sku_code,
        MAX(w.sku_name) AS sku_name,
        SUM(IFNULL(w.in_stock_quantity, 0)) AS all_stock_qty,
        SUM(IF(w.production_date > CURDATE() - INTERVAL 60 DAY, IFNULL(w.in_stock_quantity, 0), 0)) AS recent_stock_qty,
        SUM(
            CASE
                WHEN w.stock_lock IS NULL OR w.stock_lock = '' THEN 0
                ELSE CAST(w.stock_lock AS DECIMAL(20, 4))
            END
        ) AS stock_lock_qty,
        SUM(IFNULL(CAST(w.freeze_quantity AS DECIMAL(20, 4)), 0)) AS freeze_qty
    FROM ywzt_ods.scm_warehous_manage w
    WHERE (w.inventory_status = 'ZP' OR w.inventory_status IS NULL OR w.inventory_status = '')
      AND w.cargo_owner_id = 'HJXH'
    GROUP BY
        w.cargo_owner_id,
        w.wms_code,
        w.sku_code
),
occupy AS (
    SELECT
        wms_code,
        cargo_owner_id,
        sku_code,
        SUM(IFNULL(freeze_quantity, 0)) AS allocate_qty
    FROM ywzt_ods.scm_warehous_occupy
    WHERE created_date > DATE_SUB(NOW(), INTERVAL 60 DAY)
      AND cargo_owner_id = 'HJXH'
    GROUP BY
        cargo_owner_id,
        wms_code,
        sku_code
)
SELECT
    CURDATE() AS stock_date,
    inv.cargo_owner_id,
    inv.wms_code AS warehouse_code,
    mwms.wms_desc AS warehouse_name,
    inv.sku_code,
    COALESCE(ms.sku_name, inv.sku_name) AS sku_name,
    GREATEST(
        IFNULL(inv.all_stock_qty, 0)
        - IFNULL(occupy.allocate_qty, 0)
        - CASE
            WHEN IFNULL(inv.stock_lock_qty, 0) = 0 THEN IFNULL(inv.freeze_qty, 0)
            ELSE IFNULL(inv.stock_lock_qty, 0)
          END,
        0
    ) AS stock_qty,
    IFNULL(inv.all_stock_qty, 0) AS all_stock_qty,
    IFNULL(inv.recent_stock_qty, 0) AS recent_stock_qty,
    IFNULL(occupy.allocate_qty, 0) AS allocate_qty,
    IFNULL(inv.stock_lock_qty, 0) AS stock_lock_qty,
    IFNULL(inv.freeze_qty, 0) AS freeze_qty
FROM inventory inv
LEFT JOIN occupy
    ON inv.cargo_owner_id = occupy.cargo_owner_id
   AND inv.wms_code = occupy.wms_code
   AND inv.sku_code = occupy.sku_code
LEFT JOIN ywzt_ods.master_wms mwms
    ON inv.wms_code = mwms.wms_code
LEFT JOIN ywzt_ods.master_sku ms
    ON ms.sku_code = inv.sku_code
WHERE ms.sku_group = '50'
  AND mwms.wms_type IN (10, 11)
  AND mwms.wms_desc NOT LIKE '%残次%';
