# Unified Query Kernel

The chat service uses one request lifecycle for reviewed routes and model-generated
plans. Reviewed handlers are accelerators, not a separate user-facing contract.

## Request invariants

- Business timezone is `Asia/Shanghai`.
- Every time range is a half-open interval: `[start_inclusive, end_exclusive)`.
- Omitted owner, SKU, warehouse, channel, unit, and grain scope is `ALL` for the
  current request. It is never inherited from a previous request.
- A number is public only after a live Doris execution, row-count validation, and
  the metric's reconciliation checks pass.

## Learning lifecycle

User feedback and submitted definitions are candidates. They are stored in the
private learning ledger and may produce candidate contract files and regression
questions. Candidate files never change the active release. A release is active
only after contract validation, golden regression, and business review.

## Failure contract

The public layer distinguishes clarification, backend rejection, query failure,
timeout, truncation, no data, and completed results. SQL, table names, source
columns, credentials, raw rows, and audit content are private implementation data.
