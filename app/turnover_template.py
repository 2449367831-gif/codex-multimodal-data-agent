"""Reviewed, code-keyed report hierarchy; never infer ownership from names."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml
from sqlglot import exp

from .turnover_report import TurnoverReportError, _sql_literal

TEMPLATE_PATH = "profiles/hjxh_turnover_template_v3.yaml"


@dataclass(frozen=True)
class TemplateWarehouse:
    code: str
    name: str
    center_code: str
    center_name: str
    center_order: int
    detail_order: int

    @property
    def is_center(self) -> bool:
        return self.code == self.center_code


@dataclass(frozen=True)
class TurnoverTemplate:
    warehouses: tuple[TemplateWarehouse, ...]
    layout_columns: tuple[tuple[str, ...], ...]
    center_window_days: int
    detail_window_days: int

    @property
    def centers(self) -> tuple[tuple[str, str, int], ...]:
        return tuple((w.code, w.name, w.center_order) for w in self.warehouses if w.is_center)

    def scope_ctes(self, owner: str) -> str:
        values = []
        for w in self.warehouses:
            values.append(
                f"SELECT {_sql_literal(w.code)} AS wms_code, {exp.Literal.string(w.name).sql(dialect='doris')} AS expected_name, "
                f"{_sql_literal(w.center_code)} AS center_code, {exp.Literal.string(w.center_name).sql(dialect='doris')} AS center_name, "
                f"{w.center_order} AS center_order, {w.detail_order} AS detail_order, {int(w.is_center)} AS is_center"
            )
        return """report_mapping AS (
    """ + " UNION ALL ".join(values) + f"""
), warehouse_scope AS (
    SELECT {_sql_literal(owner)} AS cargo_owner_id,
        m.wms_code, COALESCE(w.warehouse_name, m.expected_name) AS warehouse_name,
        CASE WHEN m.is_center = 1 THEN '10' ELSE '13' END AS wms_node,
        w.wms_type, m.center_code AS belonging_center_code,
        m.center_name AS belonging_center, m.expected_name, m.center_order,
        m.detail_order, m.is_center,
        CASE WHEN w.wms_code IS NULL THEN 2 ELSE COALESCE(v.mapping_variant_count, 0) END AS mapping_variant_count,
        CASE WHEN COALESCE(w.candidate_center_code, '') <> m.center_code THEN 1 ELSE 0 END AS center_parent_anomaly,
        CASE WHEN w.wms_code IS NULL OR w.warehouse_name <> m.expected_name THEN 1 ELSE 0 END AS center_name_mismatch,
        w.updated_date
    FROM report_mapping AS m
    LEFT JOIN wms_latest AS w ON w.wms_code = m.wms_code
    LEFT JOIN wms_variants AS v ON v.wms_code = m.wms_code
)"""


def load_turnover_template(root: Path) -> TurnoverTemplate:
    value = yaml.safe_load((root / TEMPLATE_PATH).read_text(encoding="utf-8"))
    if (value.get("status") != "approved" or value.get("version") != "3.0.0"
            or value.get("center_window_days") != 7 or value.get("detail_window_days") != 14
            or value.get("scope_policy") != "exact_list_no_implicit_children"):
        raise TurnoverReportError("unapproved turnover template")
    warehouses = []
    for order, c in enumerate(value["centers"], 1):
        for detail_order, item in enumerate([c, *c["details"]]):
            code = str(item["code"])
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", code) or not item["name"].strip():
                raise TurnoverReportError("invalid template warehouse")
            warehouses.append(TemplateWarehouse(code, item["name"], str(c["code"]), c["name"], order, detail_order))
    centers = [w.code for w in warehouses if w.is_center]
    layout = tuple(tuple(str(c) for c in col) for col in value["layout_columns"])
    if (len(warehouses) != 51 or len(set(w.code for w in warehouses)) != 51
            or len(centers) != 9 or len(layout) != 3
            or sorted(c for col in layout for c in col) != sorted(centers)):
        raise TurnoverReportError("template must contain nine unique centers and 42 unique details")
    return TurnoverTemplate(tuple(warehouses), layout, 7, 14)
