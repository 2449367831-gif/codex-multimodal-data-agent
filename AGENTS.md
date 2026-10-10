# Doris Query Agent Rules

- `metrics/*.yaml` and `sources/**/*.yaml` are the business semantic authority.
- For natural-language requests, the model selects applicable rules after understanding
  the question. Retrieval supplies candidates, not routing decisions or mandatory execution.
  Only an explicit user-selected rule form may bypass model planning.
- Establish the metric definition, source tables, joins, grain, time window, unit,
  exclusions, freshness, and reconciliation rules before presenting a number.
- Use only `doris_query_agent.query_doris_sql` for database access.
- Model-generated SQL may be any single `SELECT` or `WITH ... SELECT` accepted by the backend.
- Never bypass the SQL AST, schema/catalog allowlist, full table qualification, read-only account,
  timeout, row cap, or audit gate.
- Rebuild owner, SKU, warehouse, channel, date, unit, and grain scope for every request.
  Unmentioned scope is `ALL`; do not inherit scope from an earlier question.
- Distinguish clarification, backend rejection, query failure, truncation, and live results.
  A generated SQL string or HTTP health response is not a Doris business result.
- When business meaning is ambiguous, return every decision needed for the current turn in one
  selectable questions array. Do not ask users to type clarification text or ask one question at a
  time; do not guess a missing variant.
- Do not expose database credentials, result rows, or audit contents in files.
- Do not use shell, web, file-edit, or other MCP tools to answer a data question.
