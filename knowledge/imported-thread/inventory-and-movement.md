# Inventory And Movement

## Real-Time Available Inventory

- Source: `ywzt_ods.scm_warehous_manage`.
- Grain: cargo owner + warehouse + SKU.
- Occupation source: `ywzt_ods.scm_warehous_occupy`, aggregated on the same key, usually within a defensive 60-day window.
- Do not call recent-production-date stock “available stock”.
- Floor negative calculated availability at zero.
- Occupation, lock, freeze, allocation, and move-in fields vary by maintained report. Read the selected SQL contract before applying them.

## Historical Inventory And Transit

- Historical inventory: `ywzt_ods.scm_warehous_manage_backups`, grain `backups_date + cargo_owner_id + wms_code + sku_code`, measure `SUM(in_stock_quantity)`.
- Historical transit: `ywzt_ods.scm_in_transit_backup`, measure `SUM(ship_qty)`, covering purchasing and transfer transit.
- Build the union of inventory and transit keys before joining so transit-only rows survive.
- Check `MAX(backups_date)` before use; previously observed 2026 transit data was stale.

Current transit has two non-equivalent business variants: historical-report compatibility may count the full purchase requested quantity, while remaining-unreceived transit deducts valid receipts and applies status-specific transfer rules. Ask the user to select the variant. Neither may execute until its source tables and joins are registered as contracts.

## Unit Conversion

- Source: `ywzt_ods.master_sku_unit`.
- Box unit: `unit='40'` and `ea_convert_quantity>0`.
- Formula: `box_qty = ea_qty / ea_convert_quantity`.
- Multiple valid box sizes for one SKU are an exception, not a reason to choose `MAX` or `MIN`.
- State the rounding rule.

## Warehouse Dimensions

Verified code dictionaries:

|Code|`wms_type`|Code|`wms_node`|
|---|---|---|---|
|10|self-owned|10|central|
|11|third-party|11|turnover|
|12|cloud|12|secondary|
|13|terminal/store|13|tertiary|
|14|transit|14|cloud|
|15|virtual|15|terminal/store|
|16|factory|16|virtual|
|||17|factory|

Excluding terminal, virtual, factory, scrap, defective, sample, packaging, or store warehouses is an operating-analysis contract, not a universal source filter. Prefer confirmed codes. Name matching is an exception audit or a maintained report rule, not a general warehouse classifier.

## Turnover

```text
daily_outbound = valid_outbound_in_window / natural_day_count
turnover_days = inventory_at_required_point / daily_outbound
```

Return null and “no movement” when the denominator is zero. Explicitly state inventory point, outbound window, included order types, and whether transfer is included.

For composite turnover, owner, SKU, warehouse profile, unit, and reporting grain must be identical in inventory and outbound. A prior natural month requires historical snapshot coverage; current real-time stock is never a historical numerator.
