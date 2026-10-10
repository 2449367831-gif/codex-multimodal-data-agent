# Scope Resolution and No-Silent-Inheritance Rules

## Scope Priority

For every dimension, decide scope independently:

```text
explicit current request
> explicit continuation phrase such as 还是、同样、按刚才口径
> named maintained report contract
> metric-required source constraint
> ALL
```

Prior context without a continuation phrase is not a filter.

## Required Dimensions

Resolve and record:

- owner
- date field and half-open range
- SKU
- warehouse
- channel/store/customer
- status and document type
- unit
- output grain

Do not treat a value resolved for one dimension as authority for another. For example, `HJXH` does not imply finished goods, and finished goods does not imply business warehouses.

## Default Meaning of ALL

- `owner_scope=ALL`: no owner predicate.
- `sku_scope=ALL`: no `sku_group`, name blacklist, or hardcoded SKU list.
- `warehouse_scope=ALL`: no warehouse type/node/name exclusion.
- `channel_scope=ALL`: no store/channel predicate.

ALL still includes metric validity conditions such as completed shipment statuses and deletion filters. Validity is not the same as scope.

## Continuation Detection

Carry a prior filter only when the user clearly says one of the following or equivalent:

- 还是按刚才口径
- 同样只看成品
- 货主仍然是 HJXH
- 仓库范围不变
- 继续查这个SKU

Do not carry filters merely because the questions are adjacent.

## Scope Audit Output

For sensitive or disputed totals, state both applied and intentionally not applied filters:

```yaml
applied:
  owner: HJXH
  date: yesterday 00:00 to today 00:00
  shipment_status: 700/900/701/901
not_applied:
  finished_goods: not requested
  business_warehouses: not requested
  channel: not requested
```

## Wide-vs-Narrow Comparison

When a scope filter can explain a large difference, compute comparison variants in one read-only query:

1. all SKU + all WMS
2. finished goods + all WMS
3. all SKU + business WMS
4. finished goods + business WMS

Do not choose a variant because it resembles an old number. Choose the variant dictated by the current task contract.

## Example: Yesterday Outbound

Request: “按HJXH货主昨天0点至今天0点的有效销售出库统计；吨数按实发数量乘SKU净重后换算为吨，单量按出库单去重，缺净重部分单列提示。”

Correct scope:

- owner HJXH
- shipment time yesterday 00:00 to today 00:00
- completed valid outbound
- all SKU because finished goods was not requested
- all WMS because a warehouse restriction was not requested
- tons from `ship_qty * net_weight / 1000`
- orders from distinct `outbound_code`
- missing weights listed
- no inventory table and no inventory validation

Incorrect inheritance:

- adding `sku_group='50'` because a previous turnover task used finished goods
- excluding factories and terminal WMS because a previous inventory task used business WMS
- loading real-time inventory to run a generic quality gate

## Example: Monthly Turnover

Request: “看下本月的周转，用目前的实时库存，截止到今天凌晨0点的销售出库。”

This explicitly requests a composite metric. Load current available inventory and month-to-date shipment. If earlier business confirmation says this turnover is HJXH finished goods and business WMS, apply that only when the current conversation clearly continues that named contract; otherwise state the assumed scope before calculating.
