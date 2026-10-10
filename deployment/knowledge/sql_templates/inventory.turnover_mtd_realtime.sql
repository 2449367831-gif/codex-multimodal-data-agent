-- Composite contract: metrics/inventory.turnover_mtd_realtime.yaml
-- Build inventory and sales from their own approved templates, apply the same explicit scope profile to both,
-- aggregate each component at final reporting grain, then calculate this final expression:
-- CASE WHEN outbound_ea = 0 OR complete_natural_days = 0 THEN NULL
--      ELSE available_ea / (outbound_ea / complete_natural_days) END AS turnover_days
-- The execution service must emit inventory freshness and sales freshness as separate fields.

