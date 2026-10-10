-- Contract: metrics/dms.points_redemption.yaml
-- The executor binds :start_inclusive, :end_exclusive, and :product_code.
-- The summary row is an independent control total; month rows are the public display grain.
WITH order_ranked AS (
  SELECT
    TRIM(o.sales_order_code) AS sales_order_code,
    o.paid_time,
    TRIM(CAST(o.order_type AS STRING)) AS order_type,
    TRIM(CAST(o.paid_status AS STRING)) AS paid_status,
    TRIM(CAST(o.order_status AS STRING)) AS order_status,
    TRIM(CAST(o.after_sales_status AS STRING)) AS after_sales_status,
    o.is_points_order,
    o.deleted_flag,
    o._fdl_marked_deleted,
    ROW_NUMBER() OVER (
      PARTITION BY TRIM(o.sales_order_code)
      ORDER BY o.updated_time DESC, o.version DESC, o.id DESC
    ) AS rn
  FROM dms_ods.t_sales_order AS o
  WHERE TRIM(o.sales_order_code) <> ''
),
valid_orders AS (
  SELECT
    sales_order_code,
    DATE_FORMAT(paid_time, '%Y-%m') AS month_label
  FROM order_ranked
  WHERE rn = 1
    AND paid_time >= :start_inclusive
    AND paid_time < :end_exclusive
    AND order_type = 'SO16'
    AND paid_status = '1'
    AND is_points_order = 1
    AND COALESCE(deleted_flag, 0) = 0
    AND LOWER(IFNULL(_fdl_marked_deleted, 'false')) NOT IN ('true', '1')
    AND COALESCE(order_status, '') NOT IN ('0', '100', '107', '108', '199')
    AND NOT (order_status = '101' AND COALESCE(after_sales_status, '') = '107')
),
detail_ranked AS (
  SELECT
    TRIM(d.sales_order_code) AS sales_order_code,
    TRIM(d.item_code) AS item_code,
    TRIM(d.sku_code) AS sku_code,
    NULLIF(TRIM(d.sku_name), '') AS sku_name,
    TRIM(CAST(d.item_type AS STRING)) AS item_type,
    d.bag_quantity,
    d.deleted_flag,
    d._fdl_marked_deleted,
    ROW_NUMBER() OVER (
      PARTITION BY TRIM(d.sales_order_code), TRIM(d.item_code), TRIM(d.sku_code)
      ORDER BY d.updated_time DESC, d.version DESC, d.id DESC
    ) AS rn
  FROM dms_ods.t_sales_order_detail AS d
  WHERE TRIM(d.sales_order_code) <> ''
    AND COALESCE(TRIM(CAST(d.item_type AS STRING)), '') = '2'
    AND TRIM(d.sku_code) = :product_code
),
valid_details AS (
  SELECT
    sales_order_code,
    sku_code,
    sku_name,
    bag_quantity
  FROM detail_ranked
  WHERE rn = 1
    AND COALESCE(item_type, '') = '2'
    AND COALESCE(deleted_flag, 0) = 0
    AND LOWER(IFNULL(_fdl_marked_deleted, 'false')) NOT IN ('true', '1')
),
detail_lines AS (
  SELECT
    o.month_label,
    o.sales_order_code,
    d.sku_code,
    d.sku_name,
    SUM(COALESCE(d.bag_quantity, 0)) AS redemption_quantity
  FROM valid_orders AS o
  JOIN valid_details AS d
    ON d.sales_order_code = o.sales_order_code
  GROUP BY o.month_label, o.sales_order_code, d.sku_code, d.sku_name
),
monthly AS (
  SELECT
    month_label,
    sku_code,
    MAX(sku_name) AS sku_name,
    COUNT(DISTINCT sales_order_code) AS order_count,
    SUM(redemption_quantity) AS redemption_quantity
  FROM detail_lines
  GROUP BY month_label, sku_code
),
source_freshness AS (
  SELECT
    (SELECT MAX(updated_time) FROM dms_ods.t_sales_order) AS header_max_updated_time,
    (SELECT MAX(updated_time) FROM dms_ods.t_sales_order_detail) AS detail_max_updated_time
),
detail_summary AS (
  SELECT
    COALESCE(SUM(redemption_quantity), 0) AS total_quantity,
    COUNT(DISTINCT sales_order_code) AS total_order_count,
    COUNT(DISTINCT sku_code) AS product_count,
    COUNT(DISTINCT month_label) AS month_count,
    COALESCE(SUM(CASE WHEN redemption_quantity < 0 THEN 1 ELSE 0 END), 0) AS negative_line_count,
    COUNT(*) AS logical_line_count,
    COUNT(DISTINCT NULLIF(sku_name, '')) AS name_variant_count
  FROM detail_lines
),
summary AS (
  SELECT
    ds.total_quantity,
    ds.total_order_count,
    ds.product_count,
    ds.month_count,
    ds.negative_line_count,
    ds.logical_line_count,
    ds.name_variant_count,
    sf.header_max_updated_time,
    sf.detail_max_updated_time,
    CASE
      WHEN sf.header_max_updated_time >= :end_exclusive
       AND sf.detail_max_updated_time >= :end_exclusive
      THEN 1 ELSE 0
    END AS freshness_ok
  FROM detail_summary AS ds
  CROSS JOIN source_freshness AS sf
)
SELECT
  'summary' AS grain,
  '' AS month_label,
  '' AS sku_code,
  '' AS sku_name,
  total_quantity AS redemption_quantity,
  total_order_count AS order_count,
  product_count,
  month_count,
  negative_line_count,
  logical_line_count,
  name_variant_count,
  freshness_ok,
  header_max_updated_time,
  detail_max_updated_time
FROM summary
UNION ALL
SELECT
  'month' AS grain,
  month_label,
  sku_code,
  sku_name,
  redemption_quantity,
  order_count,
  (SELECT product_count FROM summary),
  (SELECT month_count FROM summary),
  (SELECT negative_line_count FROM summary),
  (SELECT logical_line_count FROM summary),
  (SELECT name_variant_count FROM summary),
  (SELECT freshness_ok FROM summary),
  (SELECT header_max_updated_time FROM summary),
  (SELECT detail_max_updated_time FROM summary)
FROM monthly
ORDER BY CASE WHEN grain = 'summary' THEN 0 ELSE 1 END, month_label ASC;
