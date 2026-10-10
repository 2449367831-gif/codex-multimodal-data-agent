WITH
unit AS (
    SELECT
        sku_code,
        MAX(ea_convert_quantity) AS ea_convert_quantity
    FROM ywzt_ods.master_sku_unit
    WHERE unit = '40'
    GROUP BY sku_code
),

/* 原出库口径，但剔除社区团购-平台，避免与销售订单口径重复。 */
shipment_agg AS (
    SELECT
        DATE_FORMAT(h.ship_at, '%Y-%m-%d') AS 发货日期,
        h.facility_code AS 仓库编码,
        w.wms_desc AS 仓库描述,
        i.sku_code AS 商品编码,
        m.sku_name AS 商品描述,
        u.ea_convert_quantity AS 箱规,

        SUM(i.ship_qty) AS 总发货量,
        SUM(CASE WHEN s.type = 'Z004' THEN i.ship_qty ELSE 0 END) AS 线上汇总,
        SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DY' THEN i.ship_qty ELSE 0 END) AS 抖音,
        SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DYXD' THEN i.ship_qty ELSE 0 END) AS 抖音小时达,
        SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('PDD','PDDDF') THEN i.ship_qty ELSE 0 END) AS 拼多多系,
        SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('KS','KSDF') THEN i.ship_qty ELSE 0 END) AS 快手系,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code IN ('TGC','MJYX','TB','CT','MAOCHAO')
                THEN i.ship_qty ELSE 0
            END
        ) AS 淘宝系,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code IN ('JDVMI','JD','JD_MARKET')
                THEN i.ship_qty ELSE 0
            END
        ) AS 京东系,
        SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'WPH' THEN i.ship_qty ELSE 0 END) AS 唯品会,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND (
                     s.platform_code NOT IN (
                         'DY','DYXD',
                         'PDD','PDDDF',
                         'KS','KSDF',
                         'TGC','MJYX','TB','CT','MAOCHAO',
                         'JDVMI','JD','JD_MARKET',
                         'WPH'
                     )
                     OR s.platform_code IS NULL
                 )
                THEN i.ship_qty ELSE 0
            END
        ) AS 线上其它,

        SUM(CASE WHEN s.type = 'Z001' THEN i.ship_qty ELSE 0 END) AS 线下汇总,
        CAST(0 AS DECIMAL(38, 6)) AS 线下社区团购_平台,
        SUM(CASE WHEN s.type = 'Z001' AND s.price_group = '122' THEN i.ship_qty ELSE 0 END) AS 线下社区团购_分销,
        SUM(
            CASE
                WHEN s.type = 'Z001'
                 AND NOT (s.group1 = '03' OR s.price_group = '122')
                THEN i.ship_qty ELSE 0
            END
        ) AS 线下其它,
        SUM(
            CASE
                WHEN s.type NOT IN ('Z001','Z004') OR s.type IS NULL
                THEN i.ship_qty ELSE 0
            END
        ) AS 其它

    FROM ywzt_ods.scm_sales_shipment_header h
    INNER JOIN ywzt_ods.scm_sales_order_header h1
        ON h1.sales_order_code = h.ref_order_code
    INNER JOIN ywzt_ods.scm_sales_shipment_detail i
        ON i.outbound_code = h.outbound_code
    INNER JOIN ywzt_ods.master_sku m
        ON m.sku_code = i.sku_code
       AND m.sku_group = '50'
    LEFT JOIN unit u
        ON u.sku_code = i.sku_code
    LEFT JOIN ywzt_ods.master_store s
        ON s.code = h.store_code
    LEFT JOIN ywzt_ods.master_wms w
        ON w.wms_code = h.facility_code

    WHERE h.status IN ('700','900','701','901')
      AND (
          h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK')
          OR h.ref_order_type LIKE 'SO%'
      )
      AND NOT (
          COALESCE(s.type, '') = 'Z001'
          AND COALESCE(s.group1, '') = '03'
      )

    GROUP BY
        DATE_FORMAT(h.ship_at, '%Y-%m-%d'),
        h.facility_code,
        w.wms_desc,
        i.sku_code,
        m.sku_name,
        u.ea_convert_quantity
),

/*
 * 社区团购-平台替换口径：
 * 日期 = 销售订单 created - 1 天；数量 = 明细 request_qty；
 * 不限制订单头状态、明细状态和订单类型。
 */
community_order_agg AS (
    SELECT
        DATE_FORMAT(DATE_SUB(h.created, INTERVAL 1 DAY), '%Y-%m-%d') AS 发货日期,
        h.facility_code AS 仓库编码,
        w.wms_desc AS 仓库描述,
        d.sku_code AS 商品编码,
        m.sku_name AS 商品描述,
        u.ea_convert_quantity AS 箱规,

        SUM(d.request_qty) AS 总发货量,
        CAST(0 AS DECIMAL(38, 6)) AS 线上汇总,
        CAST(0 AS DECIMAL(38, 6)) AS 抖音,
        CAST(0 AS DECIMAL(38, 6)) AS 抖音小时达,
        CAST(0 AS DECIMAL(38, 6)) AS 拼多多系,
        CAST(0 AS DECIMAL(38, 6)) AS 快手系,
        CAST(0 AS DECIMAL(38, 6)) AS 淘宝系,
        CAST(0 AS DECIMAL(38, 6)) AS 京东系,
        CAST(0 AS DECIMAL(38, 6)) AS 唯品会,
        CAST(0 AS DECIMAL(38, 6)) AS 线上其它,
        SUM(d.request_qty) AS 线下汇总,
        SUM(d.request_qty) AS 线下社区团购_平台,
        CAST(0 AS DECIMAL(38, 6)) AS 线下社区团购_分销,
        CAST(0 AS DECIMAL(38, 6)) AS 线下其它,
        CAST(0 AS DECIMAL(38, 6)) AS 其它

    FROM ywzt_ods.scm_sales_order_header h
    INNER JOIN ywzt_ods.scm_sales_order_detail d
        ON d.sales_order_code = h.sales_order_code
    INNER JOIN ywzt_ods.master_sku m
        ON m.sku_code = d.sku_code
       AND m.sku_group = '50'
    LEFT JOIN unit u
        ON u.sku_code = d.sku_code
    INNER JOIN ywzt_ods.master_store s
        ON s.code = h.store_code
       AND s.type = 'Z001'
       AND s.group1 = '03'
    LEFT JOIN ywzt_ods.master_wms w
        ON w.wms_code = h.facility_code

    GROUP BY
        DATE_FORMAT(DATE_SUB(h.created, INTERVAL 1 DAY), '%Y-%m-%d'),
        h.facility_code,
        w.wms_desc,
        d.sku_code,
        m.sku_name,
        u.ea_convert_quantity
),

combined AS (
    SELECT * FROM shipment_agg
    UNION ALL
    SELECT * FROM community_order_agg
),

agg AS (
    SELECT
        发货日期,
        仓库编码,
        仓库描述,
        商品编码,
        商品描述,
        箱规,
        SUM(总发货量) AS 总发货量,
        SUM(线上汇总) AS 线上汇总,
        SUM(抖音) AS 抖音,
        SUM(抖音小时达) AS 抖音小时达,
        SUM(拼多多系) AS 拼多多系,
        SUM(快手系) AS 快手系,
        SUM(淘宝系) AS 淘宝系,
        SUM(京东系) AS 京东系,
        SUM(唯品会) AS 唯品会,
        SUM(线上其它) AS 线上其它,
        SUM(线下汇总) AS 线下汇总,
        SUM(线下社区团购_平台) AS 线下社区团购_平台,
        SUM(线下社区团购_分销) AS 线下社区团购_分销,
        SUM(线下其它) AS 线下其它,
        SUM(其它) AS 其它
    FROM combined
    GROUP BY
        发货日期,
        仓库编码,
        仓库描述,
        商品编码,
        商品描述,
        箱规
)

SELECT
    发货日期,
    仓库编码,
    仓库描述,
    商品编码,
    商品描述,
    箱规,

    总发货量,
    CAST(ROUND(总发货量 / NULLIF(箱规, 0), 0) AS BIGINT) AS 总发货箱数,
    线上汇总,
    CAST(ROUND(线上汇总 / NULLIF(箱规, 0), 0) AS BIGINT) AS 线上汇总箱数,
    抖音,
    CAST(ROUND(抖音 / NULLIF(箱规, 0), 0) AS BIGINT) AS 抖音箱数,
    抖音小时达,
    CAST(ROUND(抖音小时达 / NULLIF(箱规, 0), 0) AS BIGINT) AS 抖音小时达箱数,
    拼多多系,
    CAST(ROUND(拼多多系 / NULLIF(箱规, 0), 0) AS BIGINT) AS 拼多多系箱数,
    快手系,
    CAST(ROUND(快手系 / NULLIF(箱规, 0), 0) AS BIGINT) AS 快手系箱数,
    淘宝系,
    CAST(ROUND(淘宝系 / NULLIF(箱规, 0), 0) AS BIGINT) AS 淘宝系箱数,
    京东系,
    CAST(ROUND(京东系 / NULLIF(箱规, 0), 0) AS BIGINT) AS 京东系箱数,
    唯品会,
    CAST(ROUND(唯品会 / NULLIF(箱规, 0), 0) AS BIGINT) AS 唯品会箱数,
    线上其它,
    CAST(ROUND(线上其它 / NULLIF(箱规, 0), 0) AS BIGINT) AS 线上其它箱数,
    线下汇总,
    CAST(ROUND(线下汇总 / NULLIF(箱规, 0), 0) AS BIGINT) AS 线下汇总箱数,
    线下社区团购_平台,
    CAST(ROUND(线下社区团购_平台 / NULLIF(箱规, 0), 0) AS BIGINT) AS 线下社区团购_平台箱数,
    线下社区团购_分销,
    CAST(ROUND(线下社区团购_分销 / NULLIF(箱规, 0), 0) AS BIGINT) AS 线下社区团购_分销箱数,
    线下其它,
    CAST(ROUND(线下其它 / NULLIF(箱规, 0), 0) AS BIGINT) AS 线下其它箱数,
    其它,
    CAST(ROUND(其它 / NULLIF(箱规, 0), 0) AS BIGINT) AS 其它箱数

FROM agg;
