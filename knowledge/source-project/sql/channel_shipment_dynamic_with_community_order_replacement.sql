WITH

/*
 * 原出库口径：保留原有状态、单据类型、删除标记及渠道分类，
 * 仅剔除社区团购-平台，防止与销售订单口径重复。
 */
shipment_agg AS (
    SELECT
        DATE_FORMAT(h.ship_at, '%Y-%m-%d') AS 发货日期,
        i.sku_code AS 商品编码,
        m.sku_name AS 商品描述,

        SUM(i.ship_qty) AS 总发货量,
        SUM(CASE WHEN s.type = 'Z004' THEN i.ship_qty ELSE 0 END) AS 线上汇总,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code IN ('DY','WXSPHXD')
                THEN i.ship_qty ELSE 0
            END
        ) AS 抖音,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code = 'DYXD'
                THEN i.ship_qty ELSE 0
            END
        ) AS 抖音小时达,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code IN ('PDD','PDDDF')
                THEN i.ship_qty ELSE 0
            END
        ) AS 拼多多系,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code IN ('KS','KSDF')
                THEN i.ship_qty ELSE 0
            END
        ) AS 快手系,
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
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code = 'WPH'
                THEN i.ship_qty ELSE 0
            END
        ) AS 唯品会,
        SUM(
            CASE
                WHEN s.type = 'Z004'
                 AND s.platform_code NOT IN (
                     'WXSPHXD','DY','DYXD',
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
    INNER JOIN scm_sales_shipment_detail i
        ON i.outbound_code = h.outbound_code
    LEFT JOIN master_store s
        ON s.code = h.store_code
    LEFT JOIN master_sku m
        ON m.sku_code = i.sku_code

    WHERE h.ship_at BETWEEN [DYNAMIC_PARAMS.t1] AND [DYNAMIC_PARAMS.t2]
      AND (
          i.sku_code IN ([DYNAMIC_PARAMS.SKU编码])
          OR '' IN ([DYNAMIC_PARAMS.SKU编码])
      )
      AND h._fdl_marked_deleted = 'false'
      AND i._fdl_marked_deleted = 'false'
      AND h.status IN ('700','900','701','901')
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
        i.sku_code,
        m.sku_name
),

/*
 * 社区团购-平台替换口径：
 * 出库日期 = 销售订单创建时间 - 1天；数量 = 明细 request_qty；
 * 不限制订单头状态、明细状态和销售订单类型；
 * 仍剔除已被 FDL 标记删除的数据。
 */
community_order_agg AS (
    SELECT
        DATE_FORMAT(
            DATE_SUB(h.created, INTERVAL 1 DAY),
            '%Y-%m-%d'
        ) AS 发货日期,
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

    WHERE DATE_SUB(h.created, INTERVAL 1 DAY)
          BETWEEN [DYNAMIC_PARAMS.t1] AND [DYNAMIC_PARAMS.t2]
      AND (
          d.sku_code IN ([DYNAMIC_PARAMS.SKU编码])
          OR '' IN ([DYNAMIC_PARAMS.SKU编码])
      )
      AND h._fdl_marked_deleted = 'false'
      AND d._fdl_marked_deleted = 'false'

    GROUP BY
        DATE_FORMAT(
            DATE_SUB(h.created, INTERVAL 1 DAY),
            '%Y-%m-%d'
        ),
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
    商品编码,
    商品描述;
