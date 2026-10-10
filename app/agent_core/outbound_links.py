"""Candidate source relationship compiler, without shipment or amount measures."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import sqlglot
from sqlglot import exp

from ..sql import SqlSafetyValidator
from .semantic_types import SemanticRuntimeError, _parse_decimal, _parse_source_datetime


SOURCES = frozenset({"ywzt_ods.scm_sales_shipment_header",
                     "ywzt_ods.scm_sales_order_header", "ywzt_ods.scm_sales_order_detail"})
FIELDS = ("cargo_owner", "outbound_code", "referenced_order_code", "sales_order_code",
          "source_order_code", "detail_source_order_code", "source_platform_code",
          "ref_order_type", "order_type", "ship_at", "relationship_state")


@dataclass(frozen=True)
class OutboundLinkPlan:
    outbound_codes: tuple[str, ...] = ()
    owner: str = "ALL"
    start: date | None = None
    end: date | None = None
    page_limit: int = 500
    latest: bool = False

    def __post_init__(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Both half-open date bounds are required")
        if self.start is not None and (type(self.start) is not date or type(self.end) is not date or self.end <= self.start):
            raise ValueError("Invalid relationship date window")
        if not isinstance(self.owner, str) or not self.owner.strip() or self.owner != self.owner.strip():
            raise ValueError("Owner must be an exact nonblank identity")
        if not isinstance(self.outbound_codes, tuple) or any(
            not isinstance(v, str) or not v.strip() or v != v.strip() for v in self.outbound_codes
        ) or len(self.outbound_codes) != len(set(self.outbound_codes)):
            raise ValueError("Outbound codes must be distinct exact identities")
        if type(self.page_limit) is not int or not 1 <= self.page_limit <= 9999:
            raise ValueError("Overflow probe must remain bounded")
        if type(self.latest) is not bool:
            raise ValueError("Latest relationship mode must be explicit")


@dataclass(frozen=True)
class ReconciledOutboundLinks:
    rows: tuple[dict[str, Any], ...]
    document_count: int
    unmatched_document_count: int


def compile_outbound_links(plan: OutboundLinkPlan, *, project_root: Path | None = None) -> str:
    root = project_root or Path(__file__).resolve().parents[2]
    tree = sqlglot.parse_one((root / "sql_templates/sales.outbound_link.sql").read_text(encoding="utf-8"), read="doris")
    placeholders = list(tree.find_all(exp.Placeholder))
    if len(placeholders) != 1 or placeholders[0].name != "probe_limit":
        raise ValueError("Link template parameters disagree with plan")
    placeholders[0].replace(exp.Literal.number(plan.page_limit + 1))
    predicates = []
    if plan.owner != "ALL":
        predicates.append(exp.column("cargo_owner").eq(exp.Literal.string(plan.owner)))
    if plan.outbound_codes:
        predicates.append(exp.column("outbound_code").isin(*[exp.Literal.string(v) for v in plan.outbound_codes]))
    if plan.start is not None:
        predicates.extend([
            exp.GTE(this=exp.column("ship_at"), expression=exp.Literal.string(f"{plan.start.isoformat()} 00:00:00")),
            exp.LT(this=exp.column("ship_at"), expression=exp.Literal.string(f"{plan.end.isoformat()} 00:00:00")),
        ])
    if predicates:
        keys = next(c for c in tree.find_all(exp.CTE) if c.alias == "scoped_keys")
        keys.set("this", keys.this.where(exp.and_(*predicates)))
    if plan.latest:
        keys = next(c for c in tree.find_all(exp.CTE) if c.alias == "scoped_keys")
        selected = keys.this.copy()
        selected.set("distinct", None)
        selected = selected.where(exp.column("ship_at").is_(exp.Null()).not_())
        selected = selected.group_by("cargo_owner", "outbound_code").order_by(
            exp.Ordered(this=exp.Max(this=exp.column("ship_at")), desc=True, nulls_first=False),
            "cargo_owner", "outbound_code",
        ).limit(1)
        keys.set("this", selected)
    sql = tree.sql(dialect="doris")
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


def _count(value: Any) -> int:
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError("Invalid link control")
    return int(number)


def reconcile_outbound_links(rows: Sequence[Mapping[str, Any]], plan: OutboundLinkPlan) -> ReconciledOutboundLinks:
    if len(rows) > plan.page_limit:
        raise SemanticRuntimeError("出库关联链路结果已截断，不能作为完整结果", reason="result_truncated")
    seen = set()
    documents: dict[tuple[str, str], tuple[Any, ...]] = {}
    unmatched = set()
    displayed = []
    expected_documents = None
    try:
        for row in rows:
            if any(field not in row for field in FIELDS):
                raise ValueError("Incomplete link projection")
            owner, outbound = row["cargo_owner"], row["outbound_code"]
            if not all(isinstance(v, str) and v and v == v.strip() for v in (owner, outbound)):
                raise ValueError("Invalid compound document identity")
            if plan.owner != "ALL" and owner != plan.owner or plan.outbound_codes and outbound not in plan.outbound_codes:
                raise ValueError("Link outside requested identity scope")
            if plan.latest and row["ship_at"] is None:
                raise ValueError("Latest outbound requires a known shipment time")
            if plan.start is not None:
                day = _parse_source_datetime(row["ship_at"], timezone_name="Asia/Shanghai").date()
                if not plan.start <= day < plan.end:
                    raise ValueError("Link outside requested business date")
            if _count(row.get("matched_count")) != len(rows) or _count(row.get("header_variants")) != 1:
                raise ValueError("Incomplete or conflicting shipment relationship")
            if any(_count(row.get(k)) != 0 for k in ("header_errors", "order_errors", "detail_errors")):
                raise ValueError("Invalid logical deletion evidence")
            order_count = _count(row.get("order_variants"))
            if order_count not in (0, 1):
                raise ValueError("Conflicting source order")
            matched = order_count == 1
            if row["relationship_state"] != ("matched" if matched else "unmatched"):
                raise ValueError("Inconsistent relationship state")
            if matched:
                if not row["sales_order_code"] or row["sales_order_code"] != row["referenced_order_code"]:
                    raise ValueError("Source order identity differs from shipment reference")
            elif any(row[k] is not None for k in ("sales_order_code", "source_order_code", "detail_source_order_code", "source_platform_code", "order_type")):
                raise ValueError("Unmatched order cannot supply attributes")
            for field in ("referenced_order_code", "sales_order_code", "source_order_code", "detail_source_order_code", "source_platform_code", "ref_order_type", "order_type"):
                v = row[field]
                if v is not None and (not isinstance(v, str) or not v or v != v.strip()):
                    raise ValueError("Malformed relationship identifier")
            key = (owner, outbound)
            signature = tuple(row[k] for k in FIELDS if k != "detail_source_order_code")
            if key in documents and documents[key] != signature:
                raise ValueError("Document attributes changed between source rows")
            documents[key] = signature
            link_key = (*key, row["detail_source_order_code"])
            if link_key in seen:
                raise ValueError("Duplicated relationship row")
            seen.add(link_key)
            if not matched:
                unmatched.add(key)
            count = _count(row.get("total_document_count"))
            if expected_documents is not None and count != expected_documents:
                raise ValueError("Document controls disagree")
            expected_documents = count
            displayed.append({k: row[k] for k in FIELDS})
        if (expected_documents or 0) != len(documents):
            raise ValueError("Document controls do not reconcile")
        if plan.latest and len(documents) > 1:
            raise ValueError("Latest relationship returned more than one document")
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise SemanticRuntimeError("出库关联链路未通过完整性检查，暂未展示结果", reason="outbound_link_quality_failed") from exc
    return ReconciledOutboundLinks(tuple(displayed), len(documents), len(unmatched))
