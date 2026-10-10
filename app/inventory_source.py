"""Shared current-inventory SQL approved by the requester on 2026-10-10.

This is a compiler input, not an interceptor for model-generated queries.
The model receives the same template through the released metric contract.
"""
from pathlib import Path

import sqlglot
from sqlglot import exp

TEMPLATE = "sql_templates/inventory.current_components.sql"
INVENTORY_TABLES = (
    "scm_warehous_manage", "scm_warehous_occupy", "scm_po_header",
    "scm_po_detail", "scm_transfer_order_header", "scm_transfer_order_detail",
    "scm_sales_shipment_allocate_wms", "scm_stock_history",
)


def bind_current_inventory(sql: str, *, root: Path | None = None) -> str:
    """Bind the explicit inventory_current relation without changing its scope."""
    root = root or Path(__file__).resolve().parents[1]
    source = sqlglot.parse_one((root / TEMPLATE).read_text(encoding="utf-8"), read="doris")
    query = sqlglot.parse_one(sql, read="doris")
    if not any(t.name == "inventory_current" and not t.db for t in query.find_all(exp.Table)):
        raise ValueError("compiler must explicitly select inventory_current")
    source_with = source.args.pop("with_", None)
    prefix = list(source_with.expressions) if source_with else []
    prefix.append(exp.CTE(this=source, alias=exp.TableAlias(this=exp.to_identifier("inventory_current"))))
    prefix.append(exp.CTE(
        this=sqlglot.parse_one("SELECT COUNT(*) AS source_rows FROM ywzt_ods.scm_warehous_manage", read="doris"),
        alias=exp.TableAlias(this=exp.to_identifier("inv_base_health")),
    ))
    query_with = query.args.get("with_")
    existing = list(query_with.expressions) if query_with else []
    if {c.alias for c in prefix} & {c.alias for c in existing}:
        raise ValueError("inventory source CTE name collision")
    query.set("with_", exp.With(expressions=prefix + existing))
    # Compiler-owned quality evidence survives aggregation and UNION branches.
    # It is consumed internally, never added to a business table's columns.
    branches = list(query.find_all(exp.Select)) if isinstance(query, exp.Union) else [query]
    for branch in branches:
        if not isinstance(branch, exp.Select) or branch.find_ancestor(exp.CTE) is not None:
            continue
        branch.select(
            "(SELECT source_rows FROM inv_base_health) AS __inventory_base_source_rows",
            append=True, copy=False,
        )
    return query.sql(dialect="doris")


def compile_with_current_inventory(builder):
    """Mark a deterministic builder as a consumer of the approved source."""
    from functools import wraps

    @wraps(builder)
    def compiled(*args, **kwargs):
        return bind_current_inventory(builder(*args, **kwargs))
    return compiled
