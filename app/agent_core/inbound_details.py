"""Candidate receipt-detail compiler. No implicit procurement variant or live I/O."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

from ..sql import SqlSafetyValidator
from .semantic_types import SemanticRuntimeError, _parse_decimal, _parse_source_datetime


PURCHASE_BASES = frozenset({"purchase_order_reference", "purchase_type", "both"})
TEMPLATE = "sql_templates/purchase.received_detail.sql"
SOURCES = frozenset({
    "ywzt_ods.scm_warehous_header", "ywzt_ods.scm_warehous_detail", "ywzt_ods.master_wms",
})
DETAIL_FIELDS = (
    "cargo_owner", "inbound_code", "detail_id", "item_code", "warehouse_code",
    "warehouse_name", "sku_code", "batch_number", "production_date", "quantity",
    "quantity_unit", "actual_arrival_date",
)


@dataclass(frozen=True)
class InboundDetailPlan:
    production_start: date
    production_end: date
    arrival_before: date
    purchase_basis: str
    page_limit: int = 500
    owner: str = "ALL"
    warehouse_codes: tuple[str, ...] = ()
    sku_codes: tuple[str, ...] = ()

    def __post_init__(self):
        if any(type(value) is not date for value in (self.production_start, self.production_end, self.arrival_before)):
            raise ValueError("Receipt windows require calendar dates")
        if self.production_end <= self.production_start:
            raise ValueError("Production window must be nonempty and half open")
        if self.purchase_basis not in PURCHASE_BASES:
            raise ValueError("An explicit procurement variant is required")
        if type(self.page_limit) is not int or not 1 <= self.page_limit <= 9999:
            raise ValueError("The overflow probe must remain within the backend row cap")
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError("Receipt owner must be ALL or a nonempty identifier")
        if any(not isinstance(v, str) or not v.strip() for v in (*self.warehouse_codes, *self.sku_codes)):
            raise ValueError("Receipt scope identifiers must be nonempty strings")


@dataclass(frozen=True)
class ReconciledInboundDetails:
    rows: tuple[dict[str, Any], ...]
    row_count: int
    totals_by_unit: dict[str, Decimal]
    missing_warehouse_count: int


def compile_inbound_details(plan: InboundDetailPlan, *, contract_root: Path | None = None) -> str:
    root = contract_root or Path(__file__).resolve().parents[2]
    tree = sqlglot.parse_one((root / TEMPLATE).read_text(encoding="utf-8"), read="doris")
    bindings = {
        "production_start": exp.Literal.string(f"{plan.production_start.isoformat()} 00:00:00"),
        "production_end": exp.Literal.string(f"{plan.production_end.isoformat()} 00:00:00"),
        "arrival_before": exp.Literal.string(f"{plan.arrival_before.isoformat()} 00:00:00"),
        "purchase_basis": exp.Literal.string(plan.purchase_basis),
        "probe_limit": exp.Literal.number(plan.page_limit + 1),
    }
    placeholders = list(tree.find_all(exp.Placeholder))
    if {node.name for node in placeholders} != set(bindings):
        raise ValueError("Receipt template parameters disagree with the plan")
    for node in placeholders:
        node.replace(bindings[node.name].copy())
    predicates = []
    if plan.owner != "ALL":
        predicates.append(exp.column("cargo_owner").eq(exp.Literal.string(plan.owner)))
    for key, values in (("warehouse_code", plan.warehouse_codes), ("sku_code", plan.sku_codes)):
        if values:
            predicates.append(exp.column(key).isin(*[exp.Literal.string(value) for value in values]))
    if predicates:
        # WHERE runs before the window controls and ordinal-based page split.
        tree = tree.where(exp.and_(*predicates))
    sql = tree.sql(dialect="doris")
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


def _integer(value: Any) -> int:
    parsed = _parse_decimal(value)
    if parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError("Invalid receipt control")
    return int(parsed)


def reconcile_inbound_details(
    rows: Sequence[Mapping[str, Any]], plan: InboundDetailPlan, *, complete_limit: int | None = None,
) -> ReconciledInboundDetails:
    limit = plan.page_limit if complete_limit is None else complete_limit
    if type(limit) is not int or not 1 <= limit <= 100_000:
        raise ValueError("Invalid complete-result bound")
    if len(rows) > limit:
        raise SemanticRuntimeError("入库明细结果已截断，不能作为完整结果", reason="result_truncated")
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime.combine(plan.production_start, time.min, zone)
    end = datetime.combine(plan.production_end, time.min, zone)
    arrival_before = datetime.combine(plan.arrival_before, time.min, zone)
    seen = set()
    totals: dict[str, Decimal] = {}
    controls: dict[str, Decimal] = {}
    displayed = []
    missing_warehouse_count = 0
    try:
        for row in rows:
            if not isinstance(row, Mapping) or _integer(row.get("matched_count")) != len(rows):
                raise ValueError("Incomplete receipt result")
            key = tuple(str(row.get(k) or "").strip() for k in ("cargo_owner", "inbound_code", "detail_id"))
            if not all(key) or key in seen:
                raise ValueError("Receipt detail key is missing or duplicated")
            seen.add(key)
            if _integer(row.get("header_variants")) != 1 or _integer(row.get("detail_variants")) != 1:
                raise ValueError("Conflicting receipt facts")
            warehouse_variants = _integer(row.get("warehouse_variants"))
            if warehouse_variants > 1:
                raise ValueError("Conflicting warehouse mapping")
            if not str(row.get("warehouse_code") or "").strip() or not str(row.get("sku_code") or "").strip():
                raise ValueError("Receipt warehouse or SKU is missing")
            if plan.owner != "ALL" and key[0] != plan.owner:
                raise ValueError("Receipt owner is outside the requested scope")
            if plan.warehouse_codes and str(row["warehouse_code"]).strip() not in plan.warehouse_codes:
                raise ValueError("Receipt warehouse is outside the requested scope")
            if plan.sku_codes and str(row["sku_code"]).strip() not in plan.sku_codes:
                raise ValueError("Receipt SKU is outside the requested scope")
            name_present = bool(str(row.get("warehouse_name") or "").strip())
            if name_present != (warehouse_variants == 1):
                raise ValueError("Warehouse mapping control disagrees with name")
            missing_warehouse_count += int(not name_present)
            production = _parse_source_datetime(row.get("production_date"), timezone_name="Asia/Shanghai")
            arrival = _parse_source_datetime(row.get("actual_arrival_date"), timezone_name="Asia/Shanghai")
            if not start <= production < end or not arrival < arrival_before:
                raise ValueError("Receipt date axis is outside the requested window")
            quantity = _parse_decimal(row.get("quantity"))
            unit = str(row.get("quantity_unit") or "").strip()
            total = _parse_decimal(row.get("unit_quantity_total"))
            if quantity < 0 or total < 0 or not unit:
                raise ValueError("Invalid receipt quantity or source unit")
            if unit in controls and controls[unit] != total:
                raise ValueError("Receipt unit totals disagree")
            controls[unit] = total
            totals[unit] = totals.get(unit, Decimal(0)) + quantity
            values = {key: row.get(key) for key in DETAIL_FIELDS}
            values["quantity"] = format(quantity, "f")
            values["quantity_unit"] = unit
            displayed.append(values)
        if controls != totals:
            raise ValueError("Receipt details do not reconcile with unit totals")
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise SemanticRuntimeError("入库明细未通过完整性与对账检查，暂未展示结果", reason="inbound_detail_quality_failed") from exc
    return ReconciledInboundDetails(tuple(displayed), len(displayed), totals, missing_warehouse_count)
