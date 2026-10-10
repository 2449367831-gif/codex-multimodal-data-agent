# Project Source Routing

Use maintained root files before deployment, handoff, original, or temporary copies.

## Pickup Availability

- `sql/realtime_stock.sql`: HJXH finished-goods real-time available inventory for the pickup workflow.
- `sql/third_party_stock_60d.sql`: latest 3D snapshot recent-production inventory; a matching zero row overrides the real-time recent-production value.
- `sql/offline_sales_7d_warehouse_sku.sql`: last seven complete natural days, warehouse + SKU aggregation.
- `sql/offline_sales_7d.sql`: auditable daily channel detail.
- `sql/sku_box_unit.sql`: box conversion.
- `scripts/refresh_pickup_data.py`: maintained merge entrypoint.

## Shipment And Channel

- `sql/channel_shipment_with_community_order_replacement.sql`: shipment plus community-order replacement, date + SKU.
- `sql/channel_shipment_warehouse_dynamic_with_community_order_replacement.sql`: dynamic warehouse version.
- `sql/channel_shipment_dynamic_with_community_order_replacement.sql`: dynamic non-warehouse version; its Douyin grouping differs from the warehouse version.
- `scripts/channel_bag_weekly_report.py` and `scripts/prepare_channel_bag_weekly_data.py`: weekly bag report; Excel mapping is the channel attribution source.
- `scripts/generate_active_sku_png.py`: finished-goods activity over the last 90 days.

## Specialized Programs

- `scripts/generate_lingshi_dashboard.py`: Lingshi Henmang-specific owner/warehouse routing and recent-production rules. Its inventory is not available inventory.
- `online_inventory_alert/inventory_alert.py`: regional substitution-group online inventory alert.
- `tiger_sausage_inventory_report/tiger_sausage_inventory.py`: configured four-SKU sausage inventory report.
- `supply-chain-cockpit/scripts/refresh_dashboard_data.py`: cockpit-specific inventory, purchasing, forecast, supplier, and risk contracts.
- `scripts/generate_warehouse_tonnage_image.py`: finished-goods inbound/outbound tonnage.
- `scripts/build_stale_inventory_with_doris.py`: stale inventory using a contract-specific 365-day shelf life.

## Deliverable SQL

- `deliverables/.../01_仓库单量预测.sql`: `scm_wms_daily_summary` warehouse/day order metrics.
- `deliverables/.../02_出库单异常寻源报表.sql`: outbound exception source tracing.
- `deliverables/.../03_线下销售单.sql`: offline order/detail; some amount fields are placeholders.
- `deliverables/.../04_销售订单明细.sql`: returns aggregated by source order + SKU; payment reduced proportionally.
- `deliverables/.../05_库存明细.sql`: available stock, transit, seven-day outbound, availability days.
- `deliverables/.../06_商品仓库平台出库统计.sql`: shipment day + warehouse + SKU + platform.
- `deliverables/.../07_商品平台出库统计.sql`: shipment day + SKU + platform.
- `deliverables/.../08_用车申请表_查询模板.sql`: field template only; no confirmed business-status contract.

## Do Not Route As Current Production

- `*_original.sql`: historical comparison only.
- Deployment and handoff copies: use only when reproducing that exact deployed artifact.
- `tmp/`: one-time investigation, not a production entrypoint.
- `recent_stock_60d.sql` and similarly named third-party files have historical naming/content overlap; select by maintained workflow, not filename similarity.
