/* 近 7 天出库与动销口径
   粒度：发货日期 + 仓库 + SKU
   用途：
   1. 线上 7 天预留库存 = SUM(线上汇总)
   2. 线下近 7 天销量 = 线下社区团购_平台 + 线下社区团购_分销 + 线下其它
   3. 线下近 7 天日均销售 = 线下近 7 天销量 / 7
*/
SELECT
    DATE_FORMAT(h.ship_at, '%Y-%m-%d') AS ship_date,
    h.facility_code AS warehouse_code,
    w.wms_desc AS warehouse_name,
    i.sku_code AS sku_code,
    m.sku_name AS sku_name,
    SUM(i.ship_qty) AS total_ship_qty,
    SUM(CASE WHEN s.type = 'Z004' THEN i.ship_qty ELSE 0 END) AS online_ship_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DY' THEN i.ship_qty ELSE 0 END) AS online_douyin_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DYXD' THEN i.ship_qty ELSE 0 END) AS online_douyin_hour_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('PDD', 'PDDDF') THEN i.ship_qty ELSE 0 END) AS online_pdd_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('KS', 'KSDF') THEN i.ship_qty ELSE 0 END) AS online_kuaishou_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('TGC', 'MJYX', 'TB', 'CT', 'MAOCHAO') THEN i.ship_qty ELSE 0 END) AS online_taobao_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('JDVMI', 'JD', 'JD_MARKET') THEN i.ship_qty ELSE 0 END) AS online_jd_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'WPH' THEN i.ship_qty ELSE 0 END) AS online_vip_qty,
    SUM(
        CASE
            WHEN s.type = 'Z004'
             AND s.platform_code NOT IN (
                'DY', 'DYXD', 'PDD', 'PDDDF', 'KS', 'KSDF',
                'TGC', 'MJYX', 'TB', 'CT', 'MAOCHAO',
                'JDVMI', 'JD', 'JD_MARKET', 'WPH'
             )
            THEN i.ship_qty
            ELSE 0
        END
    ) AS online_other_qty,
    SUM(CASE WHEN s.type = 'Z001' THEN i.ship_qty ELSE 0 END) AS offline_ship_qty,
    SUM(CASE WHEN s.type = 'Z001' AND s.group1 = '03' THEN i.ship_qty ELSE 0 END) AS offline_community_platform_qty,
    SUM(CASE WHEN s.type = 'Z001' AND s.price_group = '122' THEN i.ship_qty ELSE 0 END) AS offline_community_distribution_qty,
    SUM(CASE WHEN s.type = 'Z001' AND NOT (s.group1 = '03' OR s.price_group = '122') THEN i.ship_qty ELSE 0 END) AS offline_other_qty,
    SUM(CASE WHEN s.type NOT IN ('Z001', 'Z004') OR s.type IS NULL THEN i.ship_qty ELSE 0 END) AS other_ship_qty,
    SUM(CASE WHEN s.type = 'Z001' AND s.group1 = '03' THEN i.ship_qty ELSE 0 END)
      + SUM(CASE WHEN s.type = 'Z001' AND s.price_group = '122' THEN i.ship_qty ELSE 0 END)
      + SUM(CASE WHEN s.type = 'Z001' AND NOT (s.group1 = '03' OR s.price_group = '122') THEN i.ship_qty ELSE 0 END) AS offline_sales_7d_qty
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
    m.sku_name;
