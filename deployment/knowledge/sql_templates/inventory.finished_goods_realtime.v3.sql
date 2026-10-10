-- Generated projection of inventory.current_components.sql; keep component semantics identical.
WITH inventory_result AS (
WITH inv_tall AS (
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

inv_w AS (
    SELECT
        wms_code,
        cargo_owner_id,
        sku_code,
        COUNT(*) AS stock_row_count,
        MAX(stock_change_time) AS stock_change_time,
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

inv_sales_allocate AS (
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

inv_transfer_allocate AS (
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

inv_po AS (
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

inv_tr AS (
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
)
SELECT inv_tall.cargo_owner_id AS owner, inv_tall.cargo_owner_id,
       inv_tall.wms_code, mwms.wms_desc, mwms.wms_type, inv_tall.sku_code,
       GREATEST(IFNULL(inv_w.in_stock_quantity, 0)
    - IFNULL(inv_sales_allocate.sales_allocate_qty, 0)
    - IFNULL(inv_transfer_allocate.transfer_allocate_qty, 0)
    - CASE WHEN IFNULL(inv_w.stock_lock, 0) = 0 THEN IFNULL(inv_w.freeze_qty, 0)
           ELSE IFNULL(inv_w.stock_lock, 0) END, 0) AS available_quantity,
       IFNULL(inv_po.po_in_transit, 0) + IFNULL(inv_tr.tr_in_transit, 0) AS in_transit_quantity,
       IFNULL(inv_w.in_stock_quantity, 0) AS in_stock_quantity,
       IFNULL(inv_w.stock_lock, 0) AS stock_lock,
       IFNULL(inv_w.freeze_qty, 0) AS freeze_quantity,
       IFNULL(inv_sales_allocate.sales_allocate_qty, 0) AS sales_occupation,
       IFNULL(inv_transfer_allocate.transfer_allocate_qty, 0) AS transfer_occupation,
       IFNULL(inv_po.po_in_transit, 0) AS purchase_in_transit,
       IFNULL(inv_tr.tr_in_transit, 0) AS transfer_in_transit,
       IFNULL(inv_w.stock_row_count, 0) AS stock_row_count,
       inv_w.stock_change_time,
       (SELECT COUNT(*) FROM ywzt_ods.scm_warehous_manage) AS source_stock_row_count
FROM inv_tall
INNER JOIN ywzt_ods.master_wms AS mwms ON inv_tall.wms_code = mwms.wms_code
LEFT JOIN inv_w ON inv_tall.wms_code = inv_w.wms_code
 AND inv_tall.cargo_owner_id = inv_w.cargo_owner_id AND inv_tall.sku_code = inv_w.sku_code
LEFT JOIN inv_sales_allocate ON inv_tall.cargo_owner_id = inv_sales_allocate.cargo_owner_id
 AND inv_tall.sku_code = inv_sales_allocate.sku_code AND inv_tall.wms_code = inv_sales_allocate.wms_code
LEFT JOIN inv_transfer_allocate ON inv_tall.cargo_owner_id = inv_transfer_allocate.cargo_owner_id
 AND inv_tall.sku_code = inv_transfer_allocate.sku_code AND inv_tall.wms_code = inv_transfer_allocate.wms_code
LEFT JOIN inv_po ON inv_tall.cargo_owner_id = inv_po.cargo_owner_id
 AND inv_tall.wms_code = inv_po.wms_code AND inv_tall.sku_code = inv_po.sku_code
LEFT JOIN inv_tr ON inv_tall.cargo_owner_id = inv_tr.cargo_owner_id
 AND inv_tall.sku_code = inv_tr.sku_code AND inv_tall.wms_code = inv_tr.wms_code
)
SELECT cargo_owner_id, wms_code, sku_code,
       available_quantity + in_transit_quantity AS realtime_finished_goods_ea,
       stock_change_time AS source_max_stock_change_time,
       source_stock_row_count AS __inventory_base_source_rows
FROM inventory_result AS i
WHERE EXISTS (SELECT 1 FROM ywzt_ods.master_sku AS s WHERE s.sku_code = i.sku_code AND TRIM(s.sku_group) = '50');
