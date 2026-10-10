/*
零食很忙看板当前程序 SQL
来源：scripts/generate_lingshi_dashboard.py
数据库：ywzt_ods
整理日期：2026-08-03

重要说明：
1. route CTE 由 Python 按“货主 + 源仓库 + 看板仓库 + SKU”动态生成。
2. 普通 SKU 使用 HJXH；肥大圣 SKU 5006070600000100 使用 FDS 和肥大圣仓库映射。
3. SKU 名称包含“盒”时 date_exempt=1，库存和在途不限制 75 天生产日期。
4. 下架 SKU 5001070072210220 在 Python 生成 route 前排除，因此不会进入以下 SQL。
5. 当前程序的库存 SQL 直接汇总 scm_warehous_manage.in_stock_quantity。
   它没有扣除 scm_warehous_occupy 占用、stock_lock 或 freeze_quantity。
6. 箱数、7 日均销和可用天数在 Python 中换算，不是在以下 SQL 中计算。
*/


/* ============================================================
   0. route CTE 动态结构
   ============================================================ */

/*
Python 会为每个有效的“仓库 + SKU”生成一行，结构如下：

WITH route AS (
    SELECT
        %s AS cargo_owner_id,
        %s AS source_wms_code,
        %s AS report_wms_desc,
        %s AS sku_code,
        %s AS report_sku_code,
        %s AS date_exempt
    UNION ALL
    SELECT %s, %s, %s, %s, %s, %s
    ...
)

参数示例：
SELECT
    'HJXH' AS cargo_owner_id,
    '实际源仓编码' AS source_wms_code,
    '皇家小虎长沙黄兴万纬仓xs' AS report_wms_desc,
    '某SKU编码' AS sku_code,
    '某SKU编码' AS report_sku_code,
    0 AS date_exempt

包含“盒”的 SKU 最后一列为 1，其余为 0。
*/


/* ============================================================
   1. 仓库匹配 SQL
   ============================================================ */

SELECT
    wms_code,
    wms_desc,
    wms_type
FROM master_wms
WHERE wms_desc IN (
    /* Python 根据 Excel 模板传入 10 个仓库名称 */
    %s
);

/* 精确名称未匹配时，程序再使用 LIKE 搜索候选仓库： */
SELECT
    wms_code,
    wms_desc,
    wms_type
FROM master_wms
WHERE wms_desc LIKE %s
ORDER BY wms_desc;


/* ============================================================
   2. 箱规 SQL
   unit=40 表示箱，ea_convert_quantity 表示一箱对应的 EA 数量
   ============================================================ */

SELECT
    u.sku_code,
    MAX(u.sku_name) AS sku_name,
    MAX(
        CASE
            WHEN u.unit = '40' THEN u.ea_convert_quantity
            ELSE NULL
        END
    ) AS box_spec,
    MAX(m.sku_group) AS sku_group
FROM master_sku_unit u
LEFT JOIN master_sku m
    ON m.sku_code = u.sku_code
WHERE u.sku_code IN (
    /* Python 传入当前有效看板 SKU */
    %s
)
GROUP BY u.sku_code;


/* ============================================================
   3. 库存 SQL
   普通 SKU：生产日期大于今天减 75 天，且不能是未来日期
   名称包含“盒”的 SKU：date_exempt=1，不限制生产日期
   ============================================================ */

WITH route AS (
    /* Python 动态生成 route 行，参见文件开头 */
    SELECT
        %s AS cargo_owner_id,
        %s AS source_wms_code,
        %s AS report_wms_desc,
        %s AS sku_code,
        %s AS report_sku_code,
        %s AS date_exempt
    /* UNION ALL SELECT %s, %s, %s, %s, %s, %s ... */
)
SELECT
    w.cargo_owner_id AS cargo_owner_id,
    w.wms_code AS warehouse_code,
    MAX(mw.wms_desc) AS source_warehouse_name,
    r.report_wms_desc AS warehouse_name,
    w.sku_code AS sku_code,
    r.report_sku_code AS report_sku_code,
    COALESCE(m.sku_name, MAX(w.sku_name)) AS sku_name,
    SUM(IFNULL(w.in_stock_quantity, 0)) AS recent_stock_qty,
    SUM(IFNULL(w.in_stock_quantity, 0)) AS summary_qty,
    SUM(IFNULL(w.in_stock_quantity, 0)) AS in_stock_qty,
    CURDATE() AS stock_snapshot_date,
    MIN(DATE(w.production_date)) AS min_production_date,
    MAX(DATE(w.production_date)) AS max_production_date,
    COUNT(*) AS source_rows
FROM scm_warehous_manage w
LEFT JOIN master_wms mw
    ON mw.wms_code = w.wms_code
LEFT JOIN master_sku m
    ON m.sku_code = w.sku_code
INNER JOIN route r
    ON r.cargo_owner_id = w.cargo_owner_id
   AND r.source_wms_code = w.wms_code
   AND r.sku_code = w.sku_code
WHERE (
        r.date_exempt = 1
        OR (
            DATE(w.production_date) > CURDATE() - INTERVAL 75 DAY
            AND DATE(w.production_date) <= CURDATE()
        )
    )
  AND (
        w.inventory_status = 'ZP'
        OR w.inventory_status IS NULL
        OR w.inventory_status = ''
    )
  AND IFNULL(w.in_stock_quantity, 0) <> 0
  AND (m.sku_group = '50' OR r.date_exempt = 1)
GROUP BY
    w.cargo_owner_id,
    w.wms_code,
    r.report_wms_desc,
    w.sku_code,
    r.report_sku_code,
    m.sku_name;


/* ============================================================
   4. 近 7 个完整发货日动销 SQL
   运行日不计入，例如 2026-08-03 运行时：
   ship_at >= '2026-07-27' AND ship_at < '2026-08-03'
   ============================================================ */

WITH route AS (
    /* Python 动态生成 route 行，参见文件开头 */
    SELECT
        %s AS cargo_owner_id,
        %s AS source_wms_code,
        %s AS report_wms_desc,
        %s AS sku_code,
        %s AS report_sku_code,
        %s AS date_exempt
    /* UNION ALL SELECT %s, %s, %s, %s, %s, %s ... */
)
SELECT
    DATE(h.ship_at) AS ship_date,
    h.facility_code AS warehouse_code,
    MAX(w.wms_desc) AS source_warehouse_name,
    r.report_wms_desc AS warehouse_name,
    i.sku_code AS sku_code,
    r.report_sku_code AS report_sku_code,
    m.sku_name AS sku_name,
    SUM(i.ship_qty) AS total_ship_qty,
    SUM(CASE WHEN s.type = 'Z004' THEN i.ship_qty ELSE 0 END) AS online_ship_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DY' THEN i.ship_qty ELSE 0 END) AS online_douyin_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DYXD' THEN i.ship_qty ELSE 0 END) AS online_douyin_hour_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('PDD', 'PDDDF') THEN i.ship_qty ELSE 0 END) AS online_pdd_qty,
    SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('KS', 'KSDF') THEN i.ship_qty ELSE 0 END) AS online_kuaishou_qty,
    SUM(
        CASE
            WHEN s.type = 'Z004'
             AND s.platform_code IN ('TGC', 'MJYX', 'TB', 'CT', 'MAOCHAO')
            THEN i.ship_qty
            ELSE 0
        END
    ) AS online_taobao_qty,
    SUM(
        CASE
            WHEN s.type = 'Z004'
             AND s.platform_code IN ('JDVMI', 'JD', 'JD_MARKET')
            THEN i.ship_qty
            ELSE 0
        END
    ) AS online_jd_qty,
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
    SUM(
        CASE
            WHEN s.type = 'Z001'
             AND NOT (s.group1 = '03' OR s.price_group = '122')
            THEN i.ship_qty
            ELSE 0
        END
    ) AS offline_other_qty,
    SUM(
        CASE
            WHEN s.type NOT IN ('Z001', 'Z004') OR s.type IS NULL
            THEN i.ship_qty
            ELSE 0
        END
    ) AS other_ship_qty
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
INNER JOIN route r
    ON r.source_wms_code = h.facility_code
   AND r.sku_code = i.sku_code
   AND (
        h.wms_owner_code = r.cargo_owner_id
        OR (r.cargo_owner_id = 'FDS' AND h.wms_owner_code IS NULL)
    )
WHERE h.ship_at >= %s
  AND h.ship_at < %s
  AND h.status IN ('700', '900', '701', '901')
  AND (h.outbound_code LIKE 'HJXH%%' OR h.outbound_code LIKE 'FDS%%')
  AND (
        h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK')
        OR h.ref_order_type LIKE 'SO%%'
    )
GROUP BY
    DATE(h.ship_at),
    h.facility_code,
    r.report_wms_desc,
    i.sku_code,
    r.report_sku_code,
    m.sku_name;


/* ============================================================
   5. 在途 SQL：采购在途 + 调拨在途
   普通 SKU 限制 75 天；名称包含“盒”的 SKU 不限制日期
   ============================================================ */

WITH route AS (
    /* Python 动态生成 route 行，参见文件开头 */
    SELECT
        %s AS cargo_owner_id,
        %s AS source_wms_code,
        %s AS report_wms_desc,
        %s AS sku_code,
        %s AS report_sku_code,
        %s AS date_exempt
    /* UNION ALL SELECT %s, %s, %s, %s, %s, %s ... */
),
po AS (
    SELECT
        p.cargo_owner_id AS cargo_owner_id,
        p.take_delivery_wms AS warehouse_code,
        MAX(mw.wms_desc) AS source_warehouse_name,
        r.report_wms_desc AS warehouse_name,
        d.sku_code AS sku_code,
        r.report_sku_code AS report_sku_code,
        COALESCE(m.sku_name, MAX(d.sku_name)) AS sku_name,
        SUM(IFNULL(d.request_quantity, 0)) AS po_transit_qty,
        0 AS transfer_transit_qty,
        MIN(DATE(d.produc_date)) AS min_transit_production_date,
        MAX(DATE(d.produc_date)) AS max_transit_production_date,
        COUNT(*) AS po_source_rows,
        0 AS transfer_source_rows
    FROM scm_po_header p
    INNER JOIN scm_po_detail d
        ON p.po_code = d.po_code
    LEFT JOIN master_wms mw
        ON mw.wms_code = p.take_delivery_wms
    LEFT JOIN master_sku m
        ON m.sku_code = d.sku_code
    INNER JOIN route r
        ON r.cargo_owner_id = p.cargo_owner_id
       AND r.source_wms_code = p.take_delivery_wms
       AND r.sku_code = d.sku_code
    WHERE p.order_status IN ('12', '13', '14', '15', '16')
      AND d.status = 0
      AND (
            r.date_exempt = 1
            OR (
                DATE(d.produc_date) > CURDATE() - INTERVAL 75 DAY
                AND DATE(d.produc_date) <= CURDATE()
            )
        )
      AND (
            d.inventory_status = 'ZP'
            OR d.inventory_status IS NULL
            OR d.inventory_status = ''
        )
      AND IFNULL(d.request_quantity, 0) <> 0
      AND (m.sku_group = '50' OR r.date_exempt = 1)
    GROUP BY
        p.cargo_owner_id,
        p.take_delivery_wms,
        r.report_wms_desc,
        d.sku_code,
        r.report_sku_code,
        m.sku_name
),
tr AS (
    SELECT
        d.cargo_owner_id AS cargo_owner_id,
        p.receive_wms AS warehouse_code,
        MAX(mw.wms_desc) AS source_warehouse_name,
        r.report_wms_desc AS warehouse_name,
        d.sku_code AS sku_code,
        r.report_sku_code AS report_sku_code,
        COALESCE(m.sku_name, MAX(d.sku_name)) AS sku_name,
        0 AS po_transit_qty,
        SUM(IFNULL(d.allocate_qty, 0)) AS transfer_transit_qty,
        MIN(DATE(d.production_date)) AS min_transit_production_date,
        MAX(DATE(d.production_date)) AS max_transit_production_date,
        0 AS po_source_rows,
        COUNT(*) AS transfer_source_rows
    FROM scm_transfer_order_header p
    INNER JOIN scm_sales_shipment_allocate_wms d
        ON p.outbound_code = d.outbound_code
    LEFT JOIN master_wms mw
        ON mw.wms_code = p.receive_wms
    LEFT JOIN master_sku m
        ON m.sku_code = d.sku_code
    INNER JOIN route r
        ON r.cargo_owner_id = d.cargo_owner_id
       AND r.source_wms_code = p.receive_wms
       AND r.sku_code = d.sku_code
    WHERE p.order_status IN ('13', '14', '15')
      AND (
            r.date_exempt = 1
            OR (
                DATE(d.production_date) > CURDATE() - INTERVAL 75 DAY
                AND DATE(d.production_date) <= CURDATE()
            )
        )
      AND (
            d.inventory_status = 'ZP'
            OR d.inventory_status IS NULL
            OR d.inventory_status = ''
        )
      AND IFNULL(d.allocate_qty, 0) <> 0
      AND (m.sku_group = '50' OR r.date_exempt = 1)
    GROUP BY
        d.cargo_owner_id,
        p.receive_wms,
        r.report_wms_desc,
        d.sku_code,
        r.report_sku_code,
        m.sku_name
),
unioned AS (
    SELECT * FROM po
    UNION ALL
    SELECT * FROM tr
)
SELECT
    cargo_owner_id,
    warehouse_code,
    MAX(source_warehouse_name) AS source_warehouse_name,
    warehouse_name,
    sku_code,
    report_sku_code,
    MAX(sku_name) AS sku_name,
    SUM(po_transit_qty) + SUM(transfer_transit_qty) AS transit_qty,
    SUM(po_transit_qty) AS po_transit_qty,
    SUM(transfer_transit_qty) AS transfer_transit_qty,
    MIN(min_transit_production_date) AS min_transit_production_date,
    MAX(max_transit_production_date) AS max_transit_production_date,
    SUM(po_source_rows) AS po_source_rows,
    SUM(transfer_source_rows) AS transfer_source_rows
FROM unioned
GROUP BY
    cargo_owner_id,
    warehouse_code,
    warehouse_name,
    sku_code,
    report_sku_code;


/* ============================================================
   6. Python 中的换算公式
   ============================================================

库存箱数 = CEIL(recent_stock_qty / box_spec)
在途箱数 = CEIL(transit_qty / box_spec)
7日总销量箱数 = CEIL(total_ship_qty / box_spec)
7日均销箱数 = CEIL(7日总销量箱数 / 7)
可用天数 = CEIL(库存箱数 / 7日均销箱数)
含在途天数 = CEIL((库存箱数 + 在途箱数) / 7日均销箱数)

当前程序下架排除：
5001070072210220  皇家小虎黑椒牛肉披萨0220G21

当前截图隐藏但 Excel 保留：
5001010070000125  皇家小虎一口爆汁肠0125G00
名称包含“芝士”的 SKU
*/
