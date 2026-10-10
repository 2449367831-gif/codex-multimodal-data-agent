/*
常温酱料看板 - DMS 近30天付款数据

输出一张图表数据集，通过 dataset_type 区分：
1. SKU_DAILY    图1：每日各SKU付款数量趋势
2. DAILY_TOTAL  图2：每日常温订单数及付款数量
3. REGION_DAILY 图3：每日各省区订单数及酱料数量

已核对的关键口径：
- 付款日期：dms_ods.t_sales_order.paid_time
- 已付款：paid_status = '1'
- 排除暂存、未支付、已退款、已取消、已删除订单
- 待备货(order_status='101')且售后已完成退款(after_sales_status='107')时剔除
- 明细只取 item_type='1' 的正品行；item_type='3' 是结算行，数量与正品行重复，不能一起统计
- DMS的 bag_quantity 实际保存最小销售单位数量；瓶装SKU按“瓶”、袋装SKU按“袋”展示
- 全部9个SKU来自《酱料价格明细表.xlsx》；总量同时拆分瓶数和袋数，不能把袋数叫作瓶数
- 省区使用 DMS 原生字段 province_department_name
*/

WITH
params AS (
    SELECT
        DATE_SUB(CURDATE(), INTERVAL 29 DAY) AS start_date,
        CURDATE() AS end_date
),

/* 保证没有销量的日期也返回0，图表X轴始终连续30天 */
date_offset AS (
    SELECT 0 AS offset_day
    UNION ALL SELECT 1
    UNION ALL SELECT 2
    UNION ALL SELECT 3
    UNION ALL SELECT 4
    UNION ALL SELECT 5
    UNION ALL SELECT 6
    UNION ALL SELECT 7
    UNION ALL SELECT 8
    UNION ALL SELECT 9
    UNION ALL SELECT 10
    UNION ALL SELECT 11
    UNION ALL SELECT 12
    UNION ALL SELECT 13
    UNION ALL SELECT 14
    UNION ALL SELECT 15
    UNION ALL SELECT 16
    UNION ALL SELECT 17
    UNION ALL SELECT 18
    UNION ALL SELECT 19
    UNION ALL SELECT 20
    UNION ALL SELECT 21
    UNION ALL SELECT 22
    UNION ALL SELECT 23
    UNION ALL SELECT 24
    UNION ALL SELECT 25
    UNION ALL SELECT 26
    UNION ALL SELECT 27
    UNION ALL SELECT 28
    UNION ALL SELECT 29
),

date_spine AS (
    SELECT DATE_ADD(p.start_date, INTERVAL d.offset_day DAY) AS paid_date
    FROM params p
    CROSS JOIN date_offset d
),

/*
常温酱料SKU清单，来源：《酱料价格明细表.xlsx》。
业务最终确认增删SKU时，只改此处，不要用 sku_name LIKE '%酱%' 模糊筛选，
否则会混入意面、焗饭、腌料、陈列盒和组合商品。
*/
target_sku AS (
    SELECT
        'HJXH-TLJ280g' AS sku_code,
        '6973990230587' AS sku_barcode,
        '皇家小虎甜辣酱280g' AS sku_name,
        '瓶' AS quantity_unit,
        20 AS box_gauge,
        CAST(3.00 AS DECIMAL(10, 2)) AS unit_price
    UNION ALL SELECT 'XHXTSLJ280G',      '6973990234530', '皇家小虎香甜沙拉酱280g',           '瓶', 20,   CAST(3.20 AS DECIMAL(10, 2))
    UNION ALL SELECT '5001080045000280', '6973990234226', '皇家小虎蜂蜜芥末酱0280G00',       '瓶', 20,   CAST(3.60 AS DECIMAL(10, 2))
    UNION ALL SELECT 'HJXH-FQSS280g',    '6973990230594', '皇家小虎番茄沙司280g',             '瓶', 20,   CAST(2.50 AS DECIMAL(10, 2))
    UNION ALL SELECT 'HJXH-FQJ10G',      '6973990230979', '皇家小虎番茄酱10G',                '袋', 1500, CAST(0.25 AS DECIMAL(10, 2))
    UNION ALL SELECT '5001080020000002', '6973990232550', '香辣蘸料 2g',                     '袋', 1000, CAST(0.25 AS DECIMAL(10, 2))
    UNION ALL SELECT '5001080013000035', '6973990232253', '小虎新奥尔良复合调味料蜜汁味 35g', '袋', 240,  CAST(0.85 AS DECIMAL(10, 2))
    UNION ALL SELECT '5001080011000035', '6973990232260', '小虎新奥尔良复合调味料微辣味 35g', '袋', 240,  CAST(0.85 AS DECIMAL(10, 2))
    UNION ALL SELECT '5001080012000035', '6973990232277', '小虎新奥尔良复合调味料中辣味 35g', '袋', 240,  CAST(0.85 AS DECIMAL(10, 2))
),

/* 近30天有效付款订单 */
valid_order AS (
    SELECT
        DATE(o.paid_time) AS paid_date,
        o.sales_order_code,
        COALESCE(NULLIF(TRIM(o.province_department_name), ''), '未分配省区')
            AS province_department_name
    FROM dms_ods.t_sales_order o
    CROSS JOIN params p
    WHERE o.paid_time >= p.start_date
      AND o.paid_time < DATE_ADD(p.end_date, INTERVAL 1 DAY)
      AND o.owner_master = 'HJXH'
      AND o.order_type = 'SO01'
      AND o.paid_status = '1'
      AND o.order_status IS NOT NULL
      AND o.order_status NOT IN ('0', '100', '107', '108', '199')
      AND NOT (
          o.order_status = '101'
          AND IFNULL(o.after_sales_status, '') = '107'
      )
      AND IFNULL(o.deleted_flag, 0) = 0
      AND LOWER(IFNULL(o._fdl_marked_deleted, 'false')) NOT IN ('true', '1')
),

/*
最细粒度：付款日期 + 省区 + 销售订单 + SKU。
一张订单有多个酱料SKU时：
- 图2/图3的订单数使用 COUNT(DISTINCT sales_order_code)，只算1单；
- 瓶数按各SKU正常累加。
*/
order_sku_base AS (
    SELECT
        o.paid_date,
        o.province_department_name,
        o.sales_order_code,
        s.sku_code,
        s.sku_barcode,
        s.sku_name,
        s.quantity_unit,
        s.box_gauge,
        s.unit_price,
        SUM(IFNULL(d.bag_quantity, 0)) AS paid_quantity
    FROM valid_order o
    INNER JOIN dms_ods.t_sales_order_detail d
        ON d.sales_order_code = o.sales_order_code
       AND d.item_type = '1'
       AND IFNULL(d.deleted_flag, 0) = 0
       AND LOWER(IFNULL(d._fdl_marked_deleted, 'false')) NOT IN ('true', '1')
    INNER JOIN target_sku s
        ON s.sku_code = d.sku_code
    GROUP BY
        o.paid_date,
        o.province_department_name,
        o.sales_order_code,
        s.sku_code,
        s.sku_barcode,
        s.sku_name,
        s.quantity_unit,
        s.box_gauge,
        s.unit_price
),

sku_daily AS (
    SELECT
        paid_date,
        sku_code,
        sku_barcode,
        sku_name,
        quantity_unit,
        box_gauge,
        unit_price,
        COUNT(DISTINCT sales_order_code) AS paid_order_count,
        SUM(paid_quantity) AS paid_quantity,
        SUM(CASE WHEN quantity_unit = '瓶' THEN paid_quantity ELSE 0 END) AS paid_bottle_count,
        SUM(CASE WHEN quantity_unit = '袋' THEN paid_quantity ELSE 0 END) AS paid_bag_count
    FROM order_sku_base
    GROUP BY
        paid_date,
        sku_code,
        sku_barcode,
        sku_name,
        quantity_unit,
        box_gauge,
        unit_price
),

daily_total AS (
    SELECT
        paid_date,
        COUNT(DISTINCT sales_order_code) AS paid_order_count,
        SUM(paid_quantity) AS paid_quantity,
        SUM(CASE WHEN quantity_unit = '瓶' THEN paid_quantity ELSE 0 END) AS paid_bottle_count,
        SUM(CASE WHEN quantity_unit = '袋' THEN paid_quantity ELSE 0 END) AS paid_bag_count
    FROM order_sku_base
    GROUP BY
        paid_date
),

region_dim AS (
    SELECT DISTINCT province_department_name
    FROM order_sku_base
),

region_daily AS (
    SELECT
        paid_date,
        province_department_name,
        COUNT(DISTINCT sales_order_code) AS paid_order_count,
        SUM(paid_quantity) AS paid_quantity,
        SUM(CASE WHEN quantity_unit = '瓶' THEN paid_quantity ELSE 0 END) AS paid_bottle_count,
        SUM(CASE WHEN quantity_unit = '袋' THEN paid_quantity ELSE 0 END) AS paid_bag_count
    FROM order_sku_base
    GROUP BY
        paid_date,
        province_department_name
),

final_data AS (
    /* 图1：每日各SKU付款瓶数趋势 */
    SELECT
        'SKU_DAILY' AS dataset_type,
        ds.paid_date,
        s.sku_code,
        s.sku_barcode,
        s.sku_name,
        s.quantity_unit,
        s.box_gauge,
        s.unit_price,
        '' AS province_department_name,
        IFNULL(a.paid_order_count, 0) AS paid_order_count,
        IFNULL(a.paid_quantity, 0) AS paid_quantity,
        IFNULL(a.paid_bottle_count, 0) AS paid_bottle_count,
        IFNULL(a.paid_bag_count, 0) AS paid_bag_count
    FROM date_spine ds
    CROSS JOIN target_sku s
    LEFT JOIN sku_daily a
        ON a.paid_date = ds.paid_date
       AND a.sku_code = s.sku_code

    UNION ALL

    /* 图2：每日常温订单付款数量及总瓶数 */
    SELECT
        'DAILY_TOTAL' AS dataset_type,
        ds.paid_date,
        '' AS sku_code,
        '' AS sku_barcode,
        '' AS sku_name,
        '混合' AS quantity_unit,
        NULL AS box_gauge,
        NULL AS unit_price,
        '' AS province_department_name,
        IFNULL(a.paid_order_count, 0) AS paid_order_count,
        IFNULL(a.paid_quantity, 0) AS paid_quantity,
        IFNULL(a.paid_bottle_count, 0) AS paid_bottle_count,
        IFNULL(a.paid_bag_count, 0) AS paid_bag_count
    FROM date_spine ds
    LEFT JOIN daily_total a
        ON a.paid_date = ds.paid_date

    UNION ALL

    /* 图3：每日各省区订单数及酱料瓶数 */
    SELECT
        'REGION_DAILY' AS dataset_type,
        ds.paid_date,
        '' AS sku_code,
        '' AS sku_barcode,
        '' AS sku_name,
        '混合' AS quantity_unit,
        NULL AS box_gauge,
        NULL AS unit_price,
        r.province_department_name,
        IFNULL(a.paid_order_count, 0) AS paid_order_count,
        IFNULL(a.paid_quantity, 0) AS paid_quantity,
        IFNULL(a.paid_bottle_count, 0) AS paid_bottle_count,
        IFNULL(a.paid_bag_count, 0) AS paid_bag_count
    FROM date_spine ds
    CROSS JOIN region_dim r
    LEFT JOIN region_daily a
        ON a.paid_date = ds.paid_date
       AND a.province_department_name = r.province_department_name
)

SELECT
    dataset_type AS `数据集类型`,
    paid_date AS `付款日期`,
    sku_code AS `商品编码`,
    sku_barcode AS `商品条码`,
    sku_name AS `商品名称`,
    quantity_unit AS `数量单位`,
    box_gauge AS `箱规`,
    unit_price AS `单价`,
    province_department_name AS `省区`,
    paid_order_count AS `付款订单数`,
    paid_quantity AS `付款数量`,
    paid_bottle_count AS `付款瓶数`,
    paid_bag_count AS `付款袋数`
FROM final_data
ORDER BY
    paid_date,
    dataset_type,
    sku_name,
    province_department_name;
