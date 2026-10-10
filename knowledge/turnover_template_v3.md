# Nine-center turnover template

Approved by the project requester on 2026-09-20:

- The standard fourteen-day warehouse image uses seven complete days for center
  summaries and fourteen complete days for subwarehouse panels.
- Both windows end at the same exclusive local midnight in Asia/Shanghai.
- The inventory numerator remains current available finished goods plus in-transit.
- Centers include their own physical warehouse plus only the listed subwarehouses.
- The scope is nine centers and 42 subwarehouses, not an automatic expansion of
  the current master hierarchy. Each physical warehouse appears once in the total.

The reviewed warehouse codes, exact master names, report assignments and display
order live in `profiles/hjxh_turnover_template_v3.yaml`. All 51 identities were
verified against active HJXH master data, with one matching record each. There
are 23 explicit report assignments that differ from the source parent field.
The report does not update the warehouse master. In particular, Guangzhou Shunde
is a separate report center; it must not also be counted under Changsha.

## Calculation And Verification

One Doris statement reads the inventory once and aggregates the two shipment
windows separately. Actual movement uses `ship_qty`, not allocation quantity.
Each window retains its own movement control total and detail-row count.
Missing master identities, changed names, ambiguous shipments, duplicate
inventory keys, truncation or simulated data block publication of the image.
Negative inventory source facts remain signed under the existing reviewed policy;
their presence is disclosed in the result caption and image quality note.

The image shows rounded daily averages as whole EA/day values. Turnover is
calculated from unrounded totals and displayed to two decimals. The grand total
is total inventory divided by total daily movement, never an average of ratios.

## Version Boundary

The active metric is `inventory.turnover_7d_realtime@3.0.0`. Only this metric and
its new profile are patched into the prior published knowledge snapshot; other
workspace candidates are not published. The prior release remains available
through the existing knowledge rollback command.

Explicitly requested single-window lengths other than fourteen days keep their
prior behavior. W37 historical workbook reproduction remains a separate candidate;
the new template does not establish equivalence to historical inventory values.

Regression coverage: `app/tests/test_turnover_template_v3.py` plus the existing
turnover report, service, historical-reference and release-refresh suites.
Production acceptance includes a successful chat request, nine-row center table,
separate date labels, and an authenticated PNG download.
