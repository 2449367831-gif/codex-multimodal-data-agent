WITH

/* 原出库口径：剔除社区团购-平台，其他逻辑保持不变。 */
shipment_agg AS (
    SELECT
        DATE_FORMAT(h.ship_at, '%Y-%m-%d') AS 发货日期,
        h.facility_code AS 仓库编码,
        w.wms_desc AS 仓库描述,
        i.sku_code AS 商品编码,
        m.sku_name AS 商品描述,

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
                 AND s.platform_code NOT IN (
                     'DY','DYXD',
                     'PDD','PDDDF',
                     'KS','KSDF',
                     'TGC','MJYX','TB','CT','MAOCHAO',
                     'JDVMI','JD','JD_MARKET',
                     'WPH'
                 )
                THEN i.ship_qty ELSE 0
            END
        ) AS 线上其它,

        SUM(CASE WHEN s.type = 'Z001' THEN i.ship_qty ELSE 0 END) AS 线下汇总,
        CAST(0 AS DECIMAL(38, 6)) AS 线下社区团购_平台,
        SUM(
            CASE
                WHEN s.type = 'Z001'
                 AND s.price_group = '122'
                THEN i.ship_qty ELSE 0
            END
        ) AS 线下社区团购_分销,
        SUM(
            CASE
                WHEN s.type = 'Z001'
                 AND NOT (
                     s.group1 = '03'
                     OR s.price_group = '122'
                 )
                THEN i.ship_qty ELSE 0
            END
        ) AS 线下其它,
        SUM(
            CASE
                WHEN s.type NOT IN ('Z001','Z004')
                  OR s.type IS NULL
                THEN i.ship_qty ELSE 0
            END
        ) AS 其它

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

    WHERE  h.status IN ('700','900','701','901')
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
        m.sku_name
),

/*
 * 社区团购-平台替换口径：
 * 日期 = 销售订单 created - 1天；数量 = 明细 request_qty；
 * 不限制订单头状态、明细状态和订单类型。
 */
community_order_agg AS (
    SELECT
        DATE_FORMAT(
            DATE_SUB(h.created, INTERVAL 1 DAY),
            '%Y-%m-%d'
        ) AS 发货日期,
        h.facility_code AS 仓库编码,
        w.wms_desc AS 仓库描述,
        d.sku_code AS 商品编码,
        m.sku_name AS 商品描述,

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

    FROM scm_sales_order_header h
    INNER JOIN scm_sales_order_detail d
        ON d.sales_order_code = h.sales_order_code
    INNER JOIN master_store s
        ON s.code = h.store_code
       AND s.type = 'Z001'
       AND s.group1 = '03'
    LEFT JOIN master_sku m
        ON m.sku_code = d.sku_code
    LEFT JOIN master_wms w
        ON w.wms_code = h.facility_code

    WHERE COALESCE(h._fdl_marked_deleted, 'false') = 'false'
  AND COALESCE(d._fdl_marked_deleted, 'false') = 'false'
  AND d.status = 0

    GROUP BY
        DATE_FORMAT(
            DATE_SUB(h.created, INTERVAL 1 DAY),
            '%Y-%m-%d'
        ),
        h.facility_code,
        w.wms_desc,
        d.sku_code,
        m.sku_name
),

combined AS (
    SELECT * FROM shipment_agg
    UNION ALL
    SELECT * FROM community_order_agg
)

SELECT
    发货日期,
    仓库编码,
    仓库描述,
    商品编码,
    商品描述,
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
    商品描述;
