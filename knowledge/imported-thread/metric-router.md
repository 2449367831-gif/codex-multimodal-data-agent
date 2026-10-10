# Metric Router

Select one primary row before reading tables or writing SQL. Composite metrics may combine only the rows explicitly named by their formula.

|Request signal|Primary metric|Required facts|Optional dimensions|Forbidden unrelated facts|Required domain reference|
|---|---|---|---|---|---|
|销量、出库量、出库吨数、出库单量|Sales outbound|shipment header + detail; distinct sales-order validity key|SKU, WMS, store, box unit|inventory, occupation, transit, purchasing, billing|`sales-and-supply.md`|
|当前库存、实时库存、可用库存|Real-time inventory|warehouse manage; occupation only when available inventory is requested|SKU, WMS, box unit|shipment unless demand/coverage requested|`inventory-and-movement.md`|
|某日库存、历史库存|Historical stock|warehouse manage backups|SKU, WMS, box unit|real-time manage|`inventory-and-movement.md`|
|在途、历史在途|Transit|the transit source explicitly requested; freshness check mandatory|SKU, WMS|shipment and inventory unless combined output requested|`inventory-and-movement.md`|
|库存周转、覆盖天数|Turnover|one inventory source + valid outbound source|SKU, WMS|billing, DMS|`inventory-and-movement.md`, `sales-and-supply.md`|
|采购数量、采购吨数|Purchase order|PO header + detail|SKU, purchase company, WMS|shipment, inventory|`sales-and-supply.md`|
|采购入库、实际入库、生产日期入库|Warehouse receipt|warehouse header + detail|SKU, WMS|shipment unless flow report requested|`sales-and-supply.md`|
|调拨、调拨在途|Transfer|transfer header + detail, or confirmed transfer receipt/shipment source|SKU, source WMS, destination WMS|sales-order semantics|`inventory-and-movement.md`|
|退货量、实退、退货率|Return|return header + detail; sales base only when rate requested|SKU, WMS, store|inventory|`sales-and-supply.md`|
|渠道、线上线下、平台|Channel sales|sales outbound plus deduplicated store dimension|SKU, WMS|inventory unless combined dashboard requested|`sales-and-supply.md`, optional `imported-channel-contracts.md`|
|缺货登记|DMS shortage|latest-version DMS order detail|DMS order header, employee, WMS|shipment shortage proxy|`dms-shortage.md`|
|快递费|Courier billing|waybill system order, carrier bill, price as requested|store, carrier, WMS|inventory, shipment quantity unless reconciliation needs it|`billing.md`|
|耗材费|Consumables billing|submaterial order/bill/price|WMS, carrier, material SKU|inventory material usage unless explicitly compared|`billing.md`|
|仓储费|Storage billing|storage order/summary/price|WMS, bill type|inventory unless price basis reconciliation needs it|`billing.md`|
|MySQL SQL 改 Doris|Migration|tables used by the input query after mapping verification|only dimensions required by input intent|unrelated standard contracts|`mysql-to-doris-migration.md`|

## Routing Rules

1. The requested output decides the primary metric, not tables mentioned in earlier context.
2. A validation query may use metadata and dimensions, but it must not introduce an unrelated business fact.
3. A composite dashboard must compute each fact independently at its own grain, validate each source, then combine summaries.
4. If the user requests only a sales measure, inventory anomalies are irrelevant and must not appear in the result.
5. If the user requests only inventory, shipment status and source sales-order duplication are irrelevant.
6. If the user requests tons and order count, both remain sales metrics; inventory is still forbidden.

## Ambiguous Words

|Word|Do not assume|Resolve as|
|---|---|---|
|库存|available, in-stock, historical, production-date, or transit|use the current request; ask only when the distinction changes the answer|
|销量|order request or actual shipment|default to confirmed actual `ship_qty` unless a named order-based contract applies|
|单量|sales orders or outbound orders|use `outbound_code` only when the user says 出库单量; otherwise label the chosen key|
|日期|created, paid, audited, shipped, arrived, produced|use the business date owned by the metric|
|在途|purchase, transfer, combined, historical, or real-time|name the source and components|
|件数|EA or boxes|state formula and box specification|

## Stop Conditions

- No verified source exists for the requested business meaning.
- Required field or table is absent and no confirmed replacement exists.
- The only path requires a write, callback, retry, or configuration change.
- Join cardinality cannot be made deterministic and would materially change the result.

In these cases, report the exact gap instead of inventing a query.
