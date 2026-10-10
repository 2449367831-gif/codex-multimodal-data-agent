# DMS Shortage Registration

Status: live table and row semantics verified on 2026-08-25.

There is no independent shortage table in the currently visible DMS schema. Reconstruct from:

```text
dms_ods.t_sales_order
dms_ods.t_sales_order_detail
```

Keep the latest detail version:

```sql
ROW_NUMBER() OVER (
  PARTITION BY sales_order_code, item_code, sku_code
  ORDER BY updated_time DESC, created_time DESC, id DESC
)
```

- Shortage condition: latest `stock_quantity = 0`.
- Shortage bags: `box_gauge * req_box_quantity + req_bag_quantity`.
- Warehouse code: prefer detail `wms_code`, otherwise header `delivery_warehouse_code`.
- Do not use requested-minus-shipped as the shortage definition.
- Frontend endpoint clue: `/so/stockOut?flag=1`.

Return source order, customer, line, SKU, box gauge, requested boxes/bags, shortage bags, stock quantity, warehouse, creation time, and creator.

