# Metric-Specific Validation Matrix

Run common checks plus the checks in the selected metric row. A check from another row is forbidden unless the task is explicitly composite.

## Common Checks

|Check|Level|Action|
|---|---|---|
|database current time/timezone|information|report when relative dates are used|
|required tables and columns exist|blocking|stop if no confirmed replacement exists|
|business date maximum and minimum|blocking or warning|decide whether the source covers the requested period|
|dimension business-key duplication|blocking if it can multiply facts|deduplicate or stop|
|unmatched required dimension keys|warning/blocking by impact|list counts and measure impact|
|detail count, distinct business keys, total measure|information|retain for reconciliation|
|selected metric variant|blocking|do not merge conflicting historical statuses, dates, or quantity bases|
|metric-specific validation set|blocking|do not run unrelated fact checks|

## Sales Outbound

|Check|Level|Rule|
|---|---|---|
|`MAX(ship_at)` within half-open range|blocking|must be `< end_exclusive`|
|valid status and deletion flags|blocking|use the task contract|
|source sales-order existence does not multiply detail|blocking|use `EXISTS` or distinct keys|
|outbound order count|blocking|`COUNT(DISTINCT outbound_code)`|
|EA reconciliation|blocking|total EA equals weighted EA plus missing-weight EA when tons are requested|
|missing net weight|warning/blocking by share|list SKU, EA, order count|
|requested SKU/WMS scope vs broad scope|information|show variants when disputed|
|inventory upper-bound check|forbidden|not relevant to sales-only tasks|

## Real-Time Inventory

|Check|Level|Rule|
|---|---|---|
|`MAX(stock_change_time)` freshness|blocking/warning|state actual timestamp|
|stock key uniqueness after aggregation|blocking|owner+wms+sku|
|occupation key uniqueness after aggregation|blocking|same key|
|negative in-stock or upper bound|warning|list keys and totals|
|negative deduction fields|warning|list keys and fields|
|available below zero|blocking|apply `GREATEST` only after key-level aggregation|
|available exceeds in-stock+move-in|warning/blocking by cause|inspect negative deductions|
|shipment-order duplication|forbidden|not relevant unless demand is requested|

## Historical Inventory and Transit

|Check|Level|Rule|
|---|---|---|
|requested backup date exists|blocking|do not substitute nearest date silently|
|transit `MAX(backups_date)`|blocking|stale 2026 source must be disclosed|
|transit-only keys retained|blocking|use key union|
|owner+wms+sku separation|blocking|do not merge owners|

## Turnover

Run all applicable inventory and sales checks, plus:

|Check|Level|Rule|
|---|---|---|
|inventory point and sales end shown separately|blocking|do not call current inventory midnight inventory|
|complete natural-day count|blocking|zero days returns null|
|zero outbound|information|turnover null and no-sales status|
|total ratio formula|blocking|sum stock divided by sum daily outbound|
|average of ratios|forbidden|never average warehouse/SKU turnover|
|component scope equivalence|blocking|owner, SKU, warehouse profile, unit, and output grain must match|
|historical numerator coverage|blocking|prior periods require the requested snapshot coverage; never use current stock|

## Purchasing and Receipt

|Check|Level|Rule|
|---|---|---|
|requested vs received quantity distinguished|blocking|name the chosen measure|
|created vs actual arrival vs production date distinguished|blocking|use requested date field|
|receipt type identified|blocking|purchase/transfer/return/adjustment/other|
|missing net weight for tons|warning/blocking by share|list impact|

## Billing

|Check|Level|Rule|
|---|---|---|
|price effective dates|blocking|must cover business date|
|all price dimensions|blocking|carrier/WMS/route/weight/etc.|
|system-only, bill-only, matched|information|report all three|
|fee field definition|blocking|do not merge fee/extFee/actualFee/bill amount|
|page vs warehouse freshness|warning/blocking|mark source incomplete if mismatched|

## Result Status

- `usable`: all blocking checks passed; warnings quantified.
- `usable_with_warning`: blocking checks passed; a material but bounded warning remains.
- `incomplete`: missing source or dimension prevents complete measure but a partial result is useful.
- `blocked`: metric meaning, period, source, cardinality, or unit cannot be made reliable.

Never report the number of passed checks without naming the checks. “8项全部通过” is not meaningful if the wrong metric checks were selected.
