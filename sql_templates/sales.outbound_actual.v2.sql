-- Contract: metrics/sales.outbound_actual.v2.yaml
-- Render only after parameter validation. Values shown as :named_parameters are executor bindings.
WITH eligible_outbound AS (
  SELECT
    h.outbound_code,
    h.company_code,
    MAX(h.ship_at) AS source_max_ship_at
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE h.ship_at >= :start_inclusive
    AND h.ship_at < :end_exclusive
    AND h.status IN (700, 900, 701, 901)
    AND COALESCE(h.delete_flag, 0) = 0
    AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
    AND (
      h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK')
      OR h.ref_order_type LIKE 'SO%'
    )
    AND EXISTS (
      SELECT 1
      FROM ywzt_ods.scm_sales_order_header AS so
      WHERE so.sales_order_code = h.ref_order_code
        AND so.company_code = h.company_code
        AND COALESCE(so.deleted_flag, 0) = 0
        AND IFNULL(so._fdl_marked_deleted, 'false') = 'false'
    )
  GROUP BY h.company_code, h.outbound_code
)
SELECT
  SUM(d.ship_qty) AS outbound_ea,
  MAX(h.source_max_ship_at) AS source_max_ship_at,
  COUNT(DISTINCT h.company_code, h.outbound_code) AS outbound_order_count
FROM eligible_outbound AS h
JOIN ywzt_ods.scm_sales_shipment_detail AS d
  ON d.outbound_code = h.outbound_code
 AND d.company_code = h.company_code
WHERE COALESCE(d.delete_flag, 0) = 0
  AND IFNULL(d._fdl_marked_deleted, 'false') = 'false';
