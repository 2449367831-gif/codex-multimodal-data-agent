# Validation And Output

## SQL Safety

- Execute one statement only.
- Allow only `SELECT` or `WITH ... SELECT`.
- Reject DDL, DML, procedures, configuration statements, and write-capable comments or dynamic SQL.
- Use fully qualified tables.
- Use closed-open date filters.
- Apply date filters to large fact tables.

## Preflight

1. Confirm columns with `information_schema.columns` when uncertain.
2. Check maximum business date and row freshness.
3. Check dimension-key duplicates before joining.
4. Inspect deletion flags and null distribution.
5. Run a narrow sample before full aggregation.

## Knowledge Precedence

1. Explicit instruction in the current request.
2. Live-validated metric contract with matching scope.
3. Maintained project SQL/program contract.
4. Imported terminology or semantic presets.
5. Historical snapshots and reconciliation anchors.

When two sources conflict, do not average or merge them. Use the higher-precedence source, record the rejected alternative, and ask only when the choice can materially change the result.

## Result Checks

- Compare row count, distinct business key count, and measure totals.
- Report missing SKU, warehouse, owner, box size, and net weight.
- Report duplicate keys and unmatched joins.
- Verify channel components against the total.
- Preserve transit-only inventory keys.
- For billing reconciliation, report matched, system-only, and bill-only waybills with counts and amounts.
- A zero-row result is not automatically a business zero; verify dates, freshness, and filters.

## Delivery

Always provide:

1. Result.
2. Metric definition and date field.
3. Grain and unit.
4. Source tables and join keys.
5. Filters and formulas.
6. Validation evidence.
7. Full executable SQL.
8. Risks and pending confirmations that can change the result.
