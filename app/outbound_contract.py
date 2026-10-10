"""Single source of truth for completed sales-outbound semantics.

All high-frequency routes use these fragments when they must build SQL. The
values are business constants, not user input. Allocation quantities remain
available only for reconciliation and are never the outbound measure.
"""

from __future__ import annotations

from typing import Final


ACTUAL_OUTBOUND_STATUS_CODES: Final[tuple[int, ...]] = (700, 900, 701, 901)
ACTUAL_OUTBOUND_REF_ORDER_TYPES: Final[tuple[str, ...]] = (
    "JYCK",
    "HHCK",
    "BFCK",
    "QTCK",
)
ACTUAL_OUTBOUND_QUANTITY_COLUMN: Final[str] = "ship_qty"
ALLOCATION_RECONCILIATION_COLUMN: Final[str] = "allocate_qty"


def completed_status_sql(alias: str = "h") -> str:
    """Return the canonical completed-status predicate fragment."""

    if not alias.isidentifier():
        raise ValueError("SQL alias must be an identifier")
    return f"{alias}.status IN ({', '.join(str(code) for code in ACTUAL_OUTBOUND_STATUS_CODES)})"


def ref_order_type_sql(alias: str = "h") -> str:
    """Return the canonical sales-outbound source-type predicate fragment."""

    if not alias.isidentifier():
        raise ValueError("SQL alias must be an identifier")
    quoted = ", ".join(f"'{value}'" for value in ACTUAL_OUTBOUND_REF_ORDER_TYPES)
    # Source text can contain padding; filtering must match the normalized
    # projection used by the sales routes, including the SO* family.
    normalized = f"TRIM({alias}.ref_order_type)"
    return f"({normalized} IN ({quoted}) OR {normalized} LIKE 'SO%')"


def actual_quantity_sql(alias: str = "d") -> str:
    """Return the only field allowed as the actual outbound quantity."""

    if not alias.isidentifier():
        raise ValueError("SQL alias must be an identifier")
    return f"{alias}.{ACTUAL_OUTBOUND_QUANTITY_COLUMN}"


def allocation_reconciliation_sql(alias: str = "a") -> str:
    """Return the allocation field for a reconciliation-only calculation."""

    if not alias.isidentifier():
        raise ValueError("SQL alias must be an identifier")
    return f"{alias}.{ALLOCATION_RECONCILIATION_COLUMN}"
