/* 提货表整合用：近 7 天按仓库 + SKU 汇总
   线上 7 天预留库存使用 online_reserved_7d_qty。
   线下近 7 天日均销售使用 offline_avg_7d_qty。
*/
WITH shipment_by_day AS (
    SELECT
        DATE_FORMAT(h.ship_at, '%Y-%m-%d') AS ship_date,
        h.facility_code AS warehouse_code,
        w.wms_desc AS warehouse_name,
        i.sku_code AS sku_code,
        m.sku_name AS sku_name,
        SUM(CASE WHEN s.type = 'Z004' THEN i.ship_qty ELSE 0 END) AS online_ship_qty,
        SUM(CASE WHEN s.type = 'Z001' AND s.group1 = '03' THEN i.ship_qty ELSE 0 END)
          + SUM(CASE WHEN s.type = 'Z001' AND s.price_group = '122' THEN i.ship_qty ELSE 0 END)
          + SUM(CASE WHEN s.type = 'Z001' AND NOT (s.group1 = '03' OR s.price_group = '122') THEN i.ship_qty ELSE 0 END) AS offline_ship_qty
    FROM scm_sales_shipment_header h
    INNER JOIN scm_sales_order_header h1
        ON h1.sales_order_code = h.ref_order_code
    INNER JOIN scm_sales_shipment_detail i
        ON i.outbound_code = h.outbound_code
    LEFT JOIN master_store s
        ON s.code = h.store_code
    LEFT JOIN master_sku m
        ON m.sku_code = i.sku_code
    LEFT JOIN master_wms w
        ON w.wms_code = h.facility_code
    WHERE h.ship_at >= CURDATE() - INTERVAL 7 DAY
      AND h.ship_at < CURDATE()
      AND h.outbound_code LIKE 'HJXH%'
      AND h.status IN ('700', '900', '701', '901')
      AND (
          h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK')
          OR h.ref_order_type LIKE 'SO%'
      )
    GROUP BY
        DATE_FORMAT(h.ship_at, '%Y-%m-%d'),
        h.facility_code,
        w.wms_desc,
        i.sku_code,
        m.sku_name
)
SELECT
    warehouse_code,
    warehouse_name,
    sku_code,
    MAX(sku_name) AS sku_name,
    SUM(online_ship_qty) AS online_reserved_7d_qty,
    SUM(offline_ship_qty) AS offline_sales_7d_qty,
    SUM(offline_ship_qty) / 7 AS offline_avg_7d_qty
FROM shipment_by_day
GROUP BY
    warehouse_code,
    warehouse_name,
    sku_code;
