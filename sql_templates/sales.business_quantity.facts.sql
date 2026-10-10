-- Approved 2026-10-10: ordinary shipments + community-platform requested quantity.
-- Scope parameters are bound by sales_source.py; never inherited from a chat.
SELECT h.company_code AS cargo_owner, h.facility_code AS wms_code,
       w.wms_desc AS warehouse_name, i.sku_code, m.sku_name,
       h.ship_at AS business_at, i.ship_qty AS quantity,
       'shipment' AS source_kind, h.outbound_code AS document_code,
       h.ref_order_code AS sales_order_code, h.store_code,
       s.type AS store_type, s.platform_code, s.group1, s.price_group
FROM ywzt_ods.scm_sales_shipment_header h
JOIN ywzt_ods.scm_sales_order_header h1 ON h1.sales_order_code = h.ref_order_code
JOIN ywzt_ods.scm_sales_shipment_detail i ON i.outbound_code = h.outbound_code
LEFT JOIN ywzt_ods.master_store s ON s.code = h.store_code
LEFT JOIN ywzt_ods.master_sku m ON m.sku_code = i.sku_code
LEFT JOIN ywzt_ods.master_wms w ON w.wms_code = h.facility_code
WHERE h.status IN (700, 900, 701, 901)
  AND (h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK') OR h.ref_order_type LIKE 'SO%')
  AND NOT (COALESCE(s.type, '') = 'Z001' AND COALESCE(s.group1, '') = '03')
  AND h.ship_at >= :start_inclusive AND h.ship_at < :end_exclusive
UNION ALL
SELECT h.company_code AS cargo_owner, h.facility_code AS wms_code,
       w.wms_desc AS warehouse_name, d.sku_code, m.sku_name,
       DATE_SUB(h.created, INTERVAL 1 DAY) AS business_at, d.request_qty AS quantity,
       'community_order' AS source_kind, h.sales_order_code AS document_code,
       h.sales_order_code, h.store_code,
       s.type AS store_type, s.platform_code, s.group1, s.price_group
FROM ywzt_ods.scm_sales_order_header h
JOIN ywzt_ods.scm_sales_order_detail d ON d.sales_order_code = h.sales_order_code
JOIN ywzt_ods.master_store s ON s.code = h.store_code AND s.type = 'Z001' AND s.group1 = '03'
LEFT JOIN ywzt_ods.master_sku m ON m.sku_code = d.sku_code
LEFT JOIN ywzt_ods.master_wms w ON w.wms_code = h.facility_code
WHERE COALESCE(h._fdl_marked_deleted, 'false') = 'false'
  AND COALESCE(d._fdl_marked_deleted, 'false') = 'false'
  AND d.status = 0
  AND h.created >= :community_start AND h.created < :community_end
