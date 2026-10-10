# Sales And Supply

## Effective Shipment

```text
ywzt_ods.scm_sales_shipment_header h
JOIN ywzt_ods.scm_sales_shipment_detail d
  ON h.outbound_code = d.outbound_code
```

- Measure: `SUM(d.ship_qty)`.
- `scm_sales_shipment_allocate_wms.allocate_qty` is an allocation/reconciliation field only; it is never the actual completed-outbound measure.
- Completed outbound statuses confirmed in the source business thread: `700/900/701/901`.
- Common order scope: `JYCK/HHCK/BFCK/QTCK` or requirement-specific `SO%`; a generic request must not be narrowed to `JYCK` alone.
- Validate source-order existence with `EXISTS` or a distinct non-deleted sales-order key set. Never join raw sales-order header versions into shipment detail aggregation.
- Shipment date: `h.ship_at` unless a contract explicitly substitutes order-created date.
- Finished goods: `master_sku.sku_group='50'` only when requested.
- Tons: `ship_qty * master_sku.net_weight / 1000`.

## Channels

Join `master_store.code = h.store_code`.

- Online: `type='Z004'`.
- Offline: `type='Z001'`.
- Offline community platform: `group1='03'`.
- Offline community distribution: `price_group='122'`.
- Null online `platform_code` belongs to online-other.

Platforms: DY, DYXD, PDD/PDDDF, KS/KSDF, TGC/MJYX/TB/CT/MAOCHAO, JDVMI/JD/JD_MARKET, WPH. Some report-specific contracts merge `WXSPHXD` into Douyin; never apply that globally.

Some community reports replace shipment with sales-order `request_qty` and use `created - 1 day`. Label this substitution explicitly.

`master_store.channel_code` and `scm_sales_order_header.trade_created_at` exist in Doris. Their presence validates schema availability, not every imported business mapping that uses them.

## Product Category

`DW.dim_sale_sku_category_mfn` is available with:

```text
sku_code, sku_name, f_level, s_level, t_level,
bi_s_lvl, bi_t_lvl, customer_category
```

When a user supplies a category name such as a third-level product group, prefer this dimension over SKU-name keyword matching. Check duplicate `sku_code` rows before joining.

Standard finished goods use `master_sku.sku_group='50'`. A narrower “edible operating finished goods” view is a separate contract that may use an approved classification file and exception review; do not redefine all finished-goods queries with name exclusions.

## Purchasing And Inbound

- Purchase orders: `scm_po_header + scm_po_detail`.
- Purchase received tons: `received_quantity * master_sku.net_weight / 1000`.
- Inbound: `scm_warehous_header + scm_warehous_detail`.
- Actual inbound date: `actual_arrival_date`.
- Actual received quantity: `received_quantity`.
- Separate purchase and transfer inbound using confirmed `warehous_type` values.
