WITH tall AS (
    SELECT DISTINCT
        tot.wms_code,
        tot.cargo_owner_id,
        tot.sku_code
    FROM (
        SELECT
            p.take_delivery_wms AS wms_code,
            p.cargo_owner_id,
            d.sku_code
        FROM ywzt_ods.scm_po_header p
        LEFT JOIN ywzt_ods.scm_po_detail d
            ON p.po_code = d.po_code
        WHERE p.order_status IN (
            '10','11','12','13','14','15','16'
        )

        UNION

        SELECT
            scm.wms_code,
            scm.cargo_owner_id,
            scm.sku_code
        FROM ywzt_ods.scm_warehous_manage scm

        UNION

        SELECT
            wms_code,
            cargo_owner_id,
            sku_code
        FROM ywzt_ods.scm_stock_history

        UNION

        SELECT
            p.receive_wms AS wms_code,
            p.receiver_cargo_owner AS cargo_owner_id,
            d.sku_code
        FROM ywzt_ods.scm_transfer_order_header p
        LEFT JOIN ywzt_ods.scm_transfer_order_detail d
            ON p.transfer_order_code = d.transfer_order_code
        WHERE p.order_status IN (
            '13','14','15'
        )
    ) tot
    WHERE tot.wms_code IS NOT NULL
      AND tot.cargo_owner_id IS NOT NULL
      AND tot.sku_code IS NOT NULL
),

w AS (
    SELECT
        wms_code,
        cargo_owner_id,
        sku_code,
        SUM(IFNULL(in_stock_quantity, 0)) AS in_stock_quantity,
        SUM(
            CASE
                WHEN TRIM(IFNULL(stock_lock, ''))
                     REGEXP '^-?[0-9]+(\\.[0-9]+)?$'
                THEN CAST(stock_lock AS DECIMAL(18, 4))
                ELSE CAST(0 AS DECIMAL(18, 4))
            END
        ) AS stock_lock,
        SUM(IFNULL(freeze_quantity, 0)) AS freeze_qty
    FROM ywzt_ods.scm_warehous_manage
    WHERE inventory_status = 'ZP'
       OR inventory_status IS NULL
       OR inventory_status = ''
    GROUP BY
        cargo_owner_id,
        wms_code,
        sku_code
),

sales_allocate AS (
    SELECT
        wms_code,
        cargo_owner_id,
        sku_code,
        SUM(IFNULL(freeze_quantity, 0)) AS sales_allocate_qty
    FROM ywzt_ods.scm_warehous_occupy
    WHERE created_date > DATE_SUB(NOW(), INTERVAL 60 DAY)
      AND status <> 1
      AND IFNULL(pre_order_code, '') NOT LIKE 'HJXH-TH%'
    GROUP BY
        cargo_owner_id,
        sku_code,
        wms_code
),

transfer_allocate AS (
    SELECT
        wms_code,
        cargo_owner_id,
        sku_code,
        SUM(IFNULL(freeze_quantity, 0)) AS transfer_allocate_qty
    FROM ywzt_ods.scm_warehous_occupy
    WHERE created_date > DATE_SUB(NOW(), INTERVAL 60 DAY)
      AND status <> 1
      AND IFNULL(pre_order_code, '') LIKE 'HJXH-TH%'
    GROUP BY
        cargo_owner_id,
        sku_code,
        wms_code
),

po AS (
    SELECT
        p.take_delivery_wms AS wms_code,
        p.cargo_owner_id,
        d.sku_code,
        SUM(IFNULL(d.request_quantity, 0)) AS po_in_transit
    FROM ywzt_ods.scm_po_header p
    LEFT JOIN ywzt_ods.scm_po_detail d
        ON p.po_code = d.po_code
    WHERE p.order_status IN (
        '12','13','14','15','16'
    )
      AND d.status = 0
    GROUP BY
        p.cargo_owner_id,
        p.take_delivery_wms,
        d.sku_code
),

tr AS (
    SELECT
        d.cargo_owner_id AS cargo_owner_id,
        p.receive_wms AS wms_code,
        d.sku_code,
        SUM(IFNULL(d.allocate_qty, 0)) AS tr_in_transit
    FROM ywzt_ods.scm_transfer_order_header p
    LEFT JOIN ywzt_ods.scm_sales_shipment_allocate_wms d
        ON p.outbound_code = d.outbound_code
    WHERE p.order_status IN (
        '13','14','15'
    )
      AND IFNULL(d._fdl_marked_deleted, '') <> 'true'
    GROUP BY
        d.cargo_owner_id,
        p.receive_wms,
        d.sku_code
),

sumq AS (
    SELECT
        swms.sku_code,
        scm_h.company_code,
        swms.wms_code,

        SUM(
            CASE
                WHEN DATE_FORMAT(
                    scm_h.ship_at,
                    '%Y-%m-%d'
                ) = DATE_FORMAT(
                    NOW(),
                    '%Y-%m-%d'
                )
                THEN IFNULL(swms.allocate_qty, 0)
                ELSE 0
            END
        ) AS shipQty1,

        SUM(
            CASE
                WHEN DATE_FORMAT(
                    scm_h.ship_at,
                    '%Y-%m-%d'
                ) = DATE_FORMAT(
                    DATE_SUB(CURDATE(), INTERVAL 1 DAY),
                    '%Y-%m-%d'
                )
                THEN IFNULL(swms.allocate_qty, 0)
                ELSE 0
            END
        ) AS shipQty2,

        SUM(
            IFNULL(swms.allocate_qty, 0)
        ) AS shipQty3

    FROM ywzt_ods.scm_sales_shipment_header scm_h
    LEFT JOIN ywzt_ods.scm_sales_shipment_allocate_wms swms
        ON scm_h.outbound_code = swms.outbound_code
    WHERE scm_h.status IN (
        '700','900'
    )
      AND scm_h.ship_at >= DATE_SUB(
          CURDATE(),
          INTERVAL 6 DAY
      )
      AND scm_h.ship_at < DATE_ADD(
          CURDATE(),
          INTERVAL 1 DAY
      )
      AND scm_h.ref_order_type = 'JYCK'
    GROUP BY
        swms.sku_code,
        scm_h.company_code,
        swms.wms_code
)

SELECT
    tall.cargo_owner_id AS "货主",
    mwms.wms_desc,
    CASE mwms.wms_type
        WHEN '10' THEN '自有仓'
        WHEN '11' THEN '第三方仓'
        WHEN '12' THEN '云仓'
        WHEN '13' THEN '终端仓'
        WHEN '14' THEN '中转仓'
        WHEN '15' THEN '虚拟仓'
        WHEN '16' THEN '工厂仓'
        ELSE CONCAT(
            '未知类型(',
            IFNULL(CAST(mwms.wms_type AS VARCHAR), 'NULL'),
            ')'
        )
    END AS "仓库类型",

    tall.sku_code AS "SKU编码",
    ms.sku_name AS "SKU名称",

    GREATEST(
        IFNULL(w.in_stock_quantity, 0)
        - IFNULL(sales_allocate.sales_allocate_qty, 0)
        - IFNULL(transfer_allocate.transfer_allocate_qty, 0)
        - CASE
            WHEN IFNULL(w.stock_lock, 0) = 0
            THEN IFNULL(w.freeze_qty, 0)
            ELSE IFNULL(w.stock_lock, 0)
          END,
        0
    ) AS "可用数量",

    GREATEST(
        IFNULL(w.in_stock_quantity, 0)
        - IFNULL(sales_allocate.sales_allocate_qty, 0)
        - IFNULL(transfer_allocate.transfer_allocate_qty, 0)
        - CASE
            WHEN IFNULL(w.stock_lock, 0) = 0
            THEN IFNULL(w.freeze_qty, 0)
            ELSE IFNULL(w.stock_lock, 0)
          END,
        0
    )
    + IFNULL(po.po_in_transit, 0)
    + IFNULL(tr.tr_in_transit, 0)
    AS "汇总数量",

    IFNULL(w.in_stock_quantity, 0) AS "在库数量",

    IFNULL(po.po_in_transit, 0)
        + IFNULL(tr.tr_in_transit, 0)
    AS "在途数量",

    IFNULL(sales_allocate.sales_allocate_qty, 0)
    AS "分配数量",

    IFNULL(sumq.shipQty1, 0)
    AS "今日总出库数",

    IFNULL(sumq.shipQty2, 0)
    AS "昨日总出库数",

    IFNULL(sumq.shipQty3, 0)
    AS "7日内总出库数",

    IFNULL(sumq.shipQty3, 0)
    AS "近7天出库量",

    ROUND(
        IFNULL(sumq.shipQty3, 0) / 7,
        2
    ) AS "近七天平均出库数",

    ROUND(
        GREATEST(
            IFNULL(w.in_stock_quantity, 0)
            - IFNULL(sales_allocate.sales_allocate_qty, 0)
            - IFNULL(transfer_allocate.transfer_allocate_qty, 0)
            - CASE
                WHEN IFNULL(w.stock_lock, 0) = 0
                THEN IFNULL(w.freeze_qty, 0)
                ELSE IFNULL(w.stock_lock, 0)
              END,
            0
        )
        / NULLIF(
            IFNULL(sumq.shipQty3, 0) / 7,
            0
        ),
        2
    ) AS "库存可用天数",

    IFNULL(w.stock_lock, 0)
    AS "锁定数量",

    IFNULL(w.freeze_qty, 0)
    AS "冻结数量",

    IFNULL(sales_allocate.sales_allocate_qty, 0)
        + IFNULL(transfer_allocate.transfer_allocate_qty, 0)
    AS "业务中台占用数量",

    0 AS "OFS占有用数量",

    IFNULL(po.po_in_transit, 0)
    AS "采购在途",

    IFNULL(tr.tr_in_transit, 0)
    AS "调拨在途"

FROM tall

INNER JOIN ywzt_ods.master_wms mwms
    ON tall.wms_code = mwms.wms_code

LEFT JOIN w
    ON tall.wms_code = w.wms_code
   AND tall.cargo_owner_id = w.cargo_owner_id
   AND tall.sku_code = w.sku_code

LEFT JOIN sales_allocate
    ON tall.cargo_owner_id = sales_allocate.cargo_owner_id
   AND tall.sku_code = sales_allocate.sku_code
   AND tall.wms_code = sales_allocate.wms_code

LEFT JOIN transfer_allocate
    ON tall.cargo_owner_id = transfer_allocate.cargo_owner_id
   AND tall.sku_code = transfer_allocate.sku_code
   AND tall.wms_code = transfer_allocate.wms_code

LEFT JOIN po
    ON tall.cargo_owner_id = po.cargo_owner_id
   AND tall.wms_code = po.wms_code
   AND tall.sku_code = po.sku_code

LEFT JOIN tr
    ON tall.cargo_owner_id = tr.cargo_owner_id
   AND tall.sku_code = tr.sku_code
   AND tall.wms_code = tr.wms_code

LEFT JOIN sumq
    ON tall.wms_code = sumq.wms_code
   AND tall.cargo_owner_id = sumq.company_code
   AND tall.sku_code = sumq.sku_code

LEFT JOIN ywzt_ods.master_sku ms
    ON ms.sku_code = tall.sku_code;

