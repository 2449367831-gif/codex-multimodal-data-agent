WITH header_observations AS (
    SELECT DISTINCT
        TRIM(company_code) AS cargo_owner, TRIM(outbound_code) AS outbound_code,
        NULLIF(TRIM(ref_order_code), '') AS referenced_order_code,
        NULLIF(TRIM(ref_order_type), '') AS ref_order_type, ship_at,
        CASE WHEN COALESCE(delete_flag, 0) IN (0, 1)
          AND COALESCE(LOWER(TRIM(_fdl_marked_deleted)), 'false') IN ('true', 'false')
          THEN 0 ELSE 1 END AS header_errors
    FROM ywzt_ods.scm_sales_shipment_header
    WHERE COALESCE(delete_flag, 0) <> 1
      AND COALESCE(LOWER(TRIM(_fdl_marked_deleted)), 'false') <> 'true'
), scoped_keys AS (
    SELECT DISTINCT cargo_owner, outbound_code FROM header_observations
), headers AS (
    SELECT h.cargo_owner, h.outbound_code,
        MIN(h.referenced_order_code) AS referenced_order_code,
        MIN(h.ref_order_type) AS ref_order_type, MIN(h.ship_at) AS ship_at,
        COUNT(*) AS header_variants, SUM(h.header_errors) AS header_errors
    FROM header_observations h
    WHERE EXISTS (SELECT 1 FROM scoped_keys k
                  WHERE (k.cargo_owner = h.cargo_owner OR (k.cargo_owner IS NULL AND h.cargo_owner IS NULL))
                    AND (k.outbound_code = h.outbound_code OR (k.outbound_code IS NULL AND h.outbound_code IS NULL)))
    GROUP BY h.cargo_owner, h.outbound_code
), order_observations AS (
    SELECT DISTINCT TRIM(o.company_code) AS cargo_owner,
        TRIM(o.sales_order_code) AS sales_order_code,
        NULLIF(TRIM(o.source_order_code), '') AS source_order_code,
        NULLIF(TRIM(o.source_platform_code), '') AS source_platform_code,
        NULLIF(TRIM(o.order_type), '') AS order_type,
        CASE WHEN COALESCE(o.deleted_flag, 0) IN (0, 1)
          AND COALESCE(LOWER(TRIM(o._fdl_marked_deleted)), 'false') IN ('true', 'false')
          THEN 0 ELSE 1 END AS order_errors
    FROM ywzt_ods.scm_sales_order_header o
    WHERE COALESCE(o.deleted_flag, 0) <> 1
      AND COALESCE(LOWER(TRIM(o._fdl_marked_deleted)), 'false') <> 'true'
      AND EXISTS (SELECT 1 FROM headers h WHERE h.cargo_owner = TRIM(o.company_code)
                  AND h.referenced_order_code = TRIM(o.sales_order_code))
), orders AS (
    SELECT cargo_owner, sales_order_code,
        MIN(source_order_code) AS source_order_code,
        MIN(source_platform_code) AS source_platform_code, MIN(order_type) AS order_type,
        COUNT(*) AS order_variants, SUM(order_errors) AS order_errors
    FROM order_observations GROUP BY cargo_owner, sales_order_code
), detail_sources AS (
    SELECT DISTINCT TRIM(d.company_code) AS cargo_owner,
        TRIM(d.sales_order_code) AS sales_order_code,
        NULLIF(TRIM(d.source_order_code), '') AS detail_source_order_code,
        CASE WHEN COALESCE(LOWER(TRIM(d._fdl_marked_deleted)), 'false') IN ('true', 'false')
             THEN 0 ELSE 1 END AS detail_errors
    FROM ywzt_ods.scm_sales_order_detail d
    WHERE d.status = 0
      AND COALESCE(LOWER(TRIM(d._fdl_marked_deleted)), 'false') <> 'true'
      AND EXISTS (SELECT 1 FROM orders o WHERE o.cargo_owner = TRIM(d.company_code)
                  AND o.sales_order_code = TRIM(d.sales_order_code))
), detail_controls AS (
    SELECT cargo_owner, sales_order_code, SUM(detail_errors) AS detail_errors
    FROM detail_sources GROUP BY cargo_owner, sales_order_code
), links AS (
    SELECT h.cargo_owner, h.outbound_code, h.referenced_order_code,
        o.sales_order_code, o.source_order_code, d.detail_source_order_code,
        o.source_platform_code, h.ref_order_type, o.order_type, h.ship_at,
        h.header_variants, h.header_errors,
        COALESCE(o.order_variants, 0) AS order_variants,
        COALESCE(o.order_errors, 0) AS order_errors,
        COALESCE(dc.detail_errors, 0) AS detail_errors,
        CASE WHEN o.sales_order_code IS NULL THEN 'unmatched' ELSE 'matched' END AS relationship_state
    FROM headers h
    LEFT JOIN orders o ON o.cargo_owner = h.cargo_owner AND o.sales_order_code = h.referenced_order_code
    LEFT JOIN detail_sources d ON d.cargo_owner = o.cargo_owner AND d.sales_order_code = o.sales_order_code
    LEFT JOIN detail_controls dc ON dc.cargo_owner = o.cargo_owner AND dc.sales_order_code = o.sales_order_code
)
SELECT links.*, COUNT(*) OVER () AS matched_count,
    (SELECT COUNT(*) FROM headers) AS total_document_count
FROM links
ORDER BY cargo_owner, outbound_code, detail_source_order_code
LIMIT :probe_limit
