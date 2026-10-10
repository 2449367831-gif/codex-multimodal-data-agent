"""Planning and compilation for receipt-day/production-day summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re

import sqlglot
from sqlglot import exp

from ..sql import SqlSafetyValidator
from ..query_scope import QueryScope
from .semantic_types import SemanticRequest, _compact


PURCHASE_BASES = frozenset({"purchase_order_reference", "purchase_type", "both"})
PURCHASE_BASIS_LABELS = {
    "purchase_order_reference": "采购单号非空",
    "purchase_type": "按采购入库类型",
    "both": "采购单号非空且按采购入库类型",
}
SOURCES = frozenset({
    "ywzt_ods.scm_warehous_header",
    "ywzt_ods.scm_warehous_detail",
    "ywzt_ods.master_wms",
    "ywzt_ods.master_sku",
})


@dataclass(frozen=True, slots=True)
class ReceiptProductionSummaryPlan:
    arrival_start: date
    arrival_end: date
    purchase_basis: str
    finished_goods_only: bool = True

    def __post_init__(self) -> None:
        if self.arrival_end <= self.arrival_start:
            raise ValueError("receipt day window must be nonempty")
        if self.purchase_basis not in PURCHASE_BASES:
            raise ValueError("unsupported purchase basis")
        if not self.finished_goods_only:
            raise ValueError("this route requires an explicit finished-goods scope")


@dataclass(frozen=True, slots=True)
class ReceiptWarehouseProductionSummaryPlan:
    """Warehouse/production-day receipt summary used by business reports."""

    arrival_start: date
    arrival_end: date
    purchase_basis: str = "purchase_type"
    warehouse_names: tuple[str, ...] = ()
    warehouse_codes: tuple[str, ...] = ()
    owner_code: str = "ALL"

    def __post_init__(self) -> None:
        if self.arrival_end <= self.arrival_start:
            raise ValueError("receipt day window must be nonempty")
        if self.purchase_basis not in PURCHASE_BASES:
            raise ValueError("unsupported purchase basis")
        if not self.warehouse_names and not self.warehouse_codes:
            raise ValueError("warehouse scope is required")
        if self.owner_code != "ALL" and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", self.owner_code):
            raise ValueError("invalid owner code")


def _date(question: str, planner) -> date | None:
    if "昨天" in question or "昨日" in question:
        return planner._now().date() - timedelta(days=1)
    if "今天" in question or "今日" in question:
        return planner._now().date()
    return planner._parse_explicit_date(question)


def _warehouse_pairs(question: str) -> tuple[tuple[str, str], ...]:
    # Only accept explicit label:code pairs. This prevents a product code or a
    # date from being silently interpreted as a warehouse filter.
    pairs: list[tuple[str, str]] = []
    for match in re.finditer(
        r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9 _-]{0,24})\s*[:：]\s*"
        r"([A-Za-z0-9][A-Za-z0-9_-]{2,79})",
        question,
    ):
        label = re.sub(r"\s+", "", match.group(1)).strip("，,；;。")
        code = match.group(2)
        if label in {"仓库", "条件", "输出", "日期"} or re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}", code):
            continue
        pairs.append((label, code))
    return tuple(dict.fromkeys(pairs))


def receipt_production_summary_clarification(question: str) -> dict[str, object] | None:
    """Return one server-owned basis choice when receipt meaning is incomplete."""

    compact = _compact(question)
    if "采购入库" not in compact or "生产日期" not in compact:
        return None
    if not any(word in compact for word in ("汇总", "统计", "分组", "按生产日期")):
        return None
    if re.search(r"采购单号\s*(?:不为空|非空|有值|不空)", question):
        return None
    if re.search(r"入库类型\s*(?:为|=|是)?\s*10", question):
        return None
    return {
        "outcome": "clarification_required",
        "questions": [{
            "id": "purchase_basis",
            "prompt": "采购入库按哪种方式识别？",
            "selection": "single",
            "options": [
                {"id": key, "label": label}
                for key, label in PURCHASE_BASIS_LABELS.items()
            ],
        }],
    }


def receipt_production_summary_request(
    question: str,
    *,
    planner,
    selected_purchase_basis: str | None = None,
) -> SemanticRequest | None:
    compact = _compact(question)
    has_production_grouping = (
        "生产日期" in compact
        and any(word in compact for word in ("汇总", "统计", "分组", "按生产日期"))
    )
    if not ("采购入库" in compact and has_production_grouping):
        return None
    arrival_day = _date(question, planner)
    if arrival_day is None:
        return None
    if "成品" not in compact and "产成品" not in compact:
        return None
    explicit_order_reference = bool(re.search(r"采购单号\s*(?:不为空|非空|有值|不空)", question))
    explicit_purchase_type = bool(
        "入库类型" in compact
        and re.search(r"入库类型\s*(?:为|=|是)?\s*10", question)
    )
    if selected_purchase_basis is not None:
        if selected_purchase_basis not in PURCHASE_BASES:
            return None
        basis = selected_purchase_basis
    elif explicit_order_reference and explicit_purchase_type:
        basis = "both"
    elif explicit_order_reference:
        basis = "purchase_order_reference"
    elif explicit_purchase_type:
        basis = "purchase_type"
    else:
        return None
    pairs = _warehouse_pairs(question)
    codes = tuple(code for _label, code in pairs)
    labels = tuple(label for label, _code in pairs)
    owner_match = re.search(r"(?:货主|owner)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,79})", question, re.IGNORECASE)
    owner = owner_match.group(1) if owner_match else "ALL"
    scope = QueryScope(
        owner=owner,
        warehouse_codes=codes,
        warehouse_labels=labels,
        finished_goods=True,
        unit="SOURCE",
    )
    return SemanticRequest(
        route="purchase_received_by_production_date",
        start=arrival_day,
        end=arrival_day + timedelta(days=1),
        scope=scope,
        finished_goods_only=True,
        receipt_summary_plan=ReceiptProductionSummaryPlan(
            arrival_start=arrival_day,
            arrival_end=arrival_day + timedelta(days=1),
            purchase_basis=basis,
        ),
    )


def _warehouse_name_terms(question: str) -> tuple[str, ...]:
    """Extract explicit place names from phrases such as ``德州和江西成品仓``."""

    question = re.sub(
        r"(?:最近|近|过去)\s*\d+\s*(?:个)?(?:完整)?(?:自然)?(?:天|日)",
        "",
        question,
    )
    question = re.sub(r"^(?:查询|查|统计|汇总|列出|请)\s*", "", question)
    match = re.search(
        r"([\u4e00-\u9fff]{2,12}(?:(?:、|，|,|和|与|及)[\u4e00-\u9fff]{2,12})+)"
        r"(?:的)?成品仓",
        question,
    )
    if match is None:
        return ()
    values = re.split(r"(?:、|，|,|和|与|及)", match.group(1))
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def receipt_warehouse_production_summary_request(
    question: str,
    *,
    planner,
) -> SemanticRequest | None:
    """Plan the multi-day warehouse/production-date receipt report.

    This is a separate contract from the older source-unit report. The
    department's approved default is inbound type 10; an explicit purchase
    order condition can still select the alternate reviewed variant.
    """

    compact = _compact(question)
    if "采购入库" not in compact or "成品仓" not in compact:
        return None
    if not any(token in compact for token in ("按仓库", "按仓", "各仓", "仓库汇总", "仓库和")):
        # Named warehouse reports do not always repeat "按仓库".
        if not _warehouse_name_terms(question):
            return None
    if not any(token in compact for token in ("生产日期", "生产日期为空")):
        return None
    # The legacy production-date route owns the narrower "receipt/detail count
    # and actual received quantity" report. This route is selected only when
    # the business asks for the additional planned quantity, SKU count, or
    # empty-production-date control fields.
    if not any(token in compact for token in ("计划入库数量", "SKU数", "sku数", "生产日期为空")):
        return None
    interval = planner._source_interval(question)
    if interval is None or interval[1] <= interval[0]:
        return None

    explicit_order_reference = bool(re.search(r"采购单号\s*(?:不为空|非空|有值|不空)", question))
    explicit_purchase_type = bool(
        "入库类型" in compact
        and re.search(r"入库类型\s*(?:为|=|是)?\s*10", question)
    )
    if explicit_order_reference and explicit_purchase_type:
        basis = "both"
    elif explicit_order_reference:
        basis = "purchase_order_reference"
    else:
        # Approved department rule: purchase receipts are identified by type 10.
        basis = "purchase_type"

    pairs = _warehouse_pairs(question)
    codes = tuple(code for _label, code in pairs)
    labels = tuple(label for label, _code in pairs)
    if not labels:
        labels = _warehouse_name_terms(question)
    if not labels and not codes:
        return None
    owner_match = re.search(
        r"(?:货主|owner)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,79})",
        question,
        re.IGNORECASE,
    )
    owner = owner_match.group(1) if owner_match else "ALL"
    scope = QueryScope(
        owner=owner,
        warehouse_codes=codes,
        warehouse_labels=labels,
        finished_goods=True,
        unit="SOURCE",
    )
    return SemanticRequest(
        route="purchase_received_by_warehouse_production_date",
        start=interval[0],
        end=interval[1],
        scope=scope,
        finished_goods_only=True,
        receipt_warehouse_summary_plan=ReceiptWarehouseProductionSummaryPlan(
            arrival_start=interval[0],
            arrival_end=interval[1],
            purchase_basis=basis,
            warehouse_names=labels,
            warehouse_codes=codes,
            owner_code=owner,
        ),
    )


def compile_receipt_warehouse_production_summary(
    plan: ReceiptWarehouseProductionSummaryPlan,
) -> str:
    start = f"{plan.arrival_start.isoformat()} 00:00:00"
    end = f"{plan.arrival_end.isoformat()} 00:00:00"
    purchase_predicate = {
        "purchase_order_reference": "purchase_order_code <> ''",
        "purchase_type": "inbound_type = '10'",
        "both": "purchase_order_code <> '' AND inbound_type = '10'",
    }[plan.purchase_basis]
    owner_predicate = (
        f"AND TRIM(CAST(h.cargo_owner_id AS STRING)) = '{plan.owner_code.replace(chr(39), chr(39) * 2)}'"
        if plan.owner_code != "ALL" else ""
    )
    code_predicate = ""
    if plan.warehouse_codes:
        values = ", ".join("'" + code.replace("'", "''") + "'" for code in plan.warehouse_codes)
        code_predicate = f"AND wms_code IN ({values})"
    name_predicate = ""
    if plan.warehouse_names:
        terms = " OR ".join(
            "LOCATE('" + name.replace("'", "''") + "', warehouse_name) > 0"
            for name in plan.warehouse_names
        )
        name_predicate = f"AND ({terms})"
    sql = f"""WITH wms_ranked AS (
  SELECT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS wms_code,
         TRIM(COALESCE(wms_desc, '')) AS warehouse_name,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(wms_code)
           ORDER BY updated_date DESC, version DESC, id DESC
         ) AS rn
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
    AND TRIM(wms_code) <> ''
), wms_latest AS (
  SELECT cargo_owner, wms_code, warehouse_name
  FROM wms_ranked
  WHERE rn = 1
), wms_variants AS (
  SELECT cargo_owner, wms_code, COUNT(DISTINCT warehouse_name) AS name_variants
  FROM wms_ranked
  GROUP BY cargo_owner, wms_code
), warehouse_scope AS (
  SELECT l.cargo_owner, l.wms_code, l.warehouse_name,
         COALESCE(v.name_variants, 0) AS name_variants
  FROM wms_latest AS l
  LEFT JOIN wms_variants AS v
    ON v.cargo_owner = l.cargo_owner AND v.wms_code = l.wms_code
), header_source AS (
  SELECT TRIM(CAST(h.id AS STRING)) AS receipt_id,
         TRIM(CAST(h.cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(h.warehous_code) AS inbound_code,
         COALESCE(TRIM(h.po_order_code), '') AS purchase_order_code,
         COALESCE(TRIM(CAST(h.warehous_type AS STRING)), '') AS inbound_type,
         h.actual_arrival_date,
         COALESCE(TRIM(CAST(h.status AS STRING)), '') AS logical_status,
         COALESCE(TRIM(CAST(h.warehous_status AS STRING)), '') AS receipt_status,
         LOWER(COALESCE(TRIM(CAST(h._fdl_marked_deleted AS STRING)), 'false')) AS deleted
  FROM ywzt_ods.scm_warehous_header AS h
  WHERE h.actual_arrival_date >= '{start}'
    AND h.actual_arrival_date < '{end}'
    {owner_predicate}
), eligible_headers AS (
  SELECT receipt_id, cargo_owner, inbound_code,
         COUNT(*) OVER (PARTITION BY receipt_id) AS receipt_variants
  FROM header_source
  WHERE logical_status = '0'
    AND deleted = 'false'
    AND receipt_status <> '16'
    AND {purchase_predicate}
), sku_candidates AS (
  SELECT TRIM(CAST(s.sku_code AS STRING)) AS sku_code,
         COUNT(DISTINCT TRIM(CAST(s.sku_group AS STRING))) AS sku_group_variants,
         MAX(TRIM(CAST(s.sku_group AS STRING))) AS sku_group
  FROM ywzt_ods.master_sku AS s
  WHERE TRIM(CAST(s.sku_code AS STRING)) <> ''
  GROUP BY TRIM(CAST(s.sku_code AS STRING))
), finished_skus AS (
  SELECT sku_code FROM sku_candidates
  WHERE sku_group = '50' AND sku_group_variants = 1
), detail_facts AS (
  SELECT h.receipt_id, h.cargo_owner, h.inbound_code,
         TRIM(CAST(d.wms_code AS STRING)) AS wms_code,
         COALESCE(w.warehouse_name, TRIM(CAST(d.wms_code AS STRING))) AS warehouse_name,
         COALESCE(w.name_variants, 0) AS warehouse_name_variants,
         TRIM(CAST(d.id AS STRING)) AS detail_id,
         TRIM(CAST(d.sku_code AS STRING)) AS sku_code,
         d.production_date,
         TRIM(CAST(d.unit AS STRING)) AS source_unit,
         COALESCE(d.total_quantity, 0) AS planned_quantity,
         COALESCE(d.received_quantity, 0) AS received_quantity,
         h.receipt_variants
  FROM eligible_headers AS h
  JOIN ywzt_ods.scm_warehous_detail AS d
    ON TRIM(CAST(d.cargo_owner_id AS STRING)) = h.cargo_owner
   AND TRIM(d.warehous_code) = h.inbound_code
  JOIN finished_skus AS s ON s.sku_code = TRIM(CAST(d.sku_code AS STRING))
  LEFT JOIN warehouse_scope AS w
    ON w.cargo_owner = h.cargo_owner
   AND w.wms_code = TRIM(CAST(d.wms_code AS STRING))
  WHERE COALESCE(TRIM(CAST(d.status AS STRING)), '0') = '0'
    AND LOWER(COALESCE(TRIM(CAST(d._fdl_marked_deleted AS STRING)), 'false')) = 'false'
    AND TRIM(CAST(d.unit AS STRING)) <> ''
    {code_predicate.replace('wms_code', 'TRIM(CAST(d.wms_code AS STRING))')}
    {name_predicate.replace('warehouse_name', 'COALESCE(w.warehouse_name, \'\')')}
), controls AS (
  SELECT COUNT(*) AS detail_total,
         COUNT(DISTINCT CONCAT_WS('|', cargo_owner, inbound_code, detail_id)) AS detail_distinct,
         SUM(CASE WHEN production_date IS NULL THEN 1 ELSE 0 END) AS empty_production_detail_count,
         SUM(CASE WHEN planned_quantity < 0 OR received_quantity < 0 THEN 1 ELSE 0 END) AS negative_quantity_count,
         MAX(receipt_variants) AS max_receipt_variants,
         MAX(warehouse_name_variants) AS max_warehouse_name_variants
  FROM detail_facts
), grouped AS (
  SELECT cargo_owner, wms_code, warehouse_name,
         CASE WHEN production_date IS NULL THEN '生产日期为空'
              ELSE DATE_FORMAT(production_date, '%Y-%m-%d') END AS production_day,
         source_unit,
         COUNT(DISTINCT inbound_code) AS receipt_count,
         COUNT(DISTINCT sku_code) AS sku_count,
         SUM(planned_quantity) AS planned_quantity,
         SUM(received_quantity) AS received_quantity,
         SUM(CASE WHEN production_date IS NULL THEN 1 ELSE 0 END) AS empty_production_detail_count,
         COUNT(*) AS detail_count
  FROM detail_facts
  GROUP BY cargo_owner, wms_code, warehouse_name,
           CASE WHEN production_date IS NULL THEN '生产日期为空'
                ELSE DATE_FORMAT(production_date, '%Y-%m-%d') END,
           source_unit
)
SELECT g.cargo_owner, g.wms_code, g.warehouse_name, g.production_day,
       g.source_unit, g.receipt_count, g.sku_count, g.planned_quantity,
       g.received_quantity, g.empty_production_detail_count, g.detail_count,
       c.detail_total, c.detail_distinct, c.empty_production_detail_count AS control_empty_production_detail_count,
       c.negative_quantity_count, c.max_receipt_variants, c.max_warehouse_name_variants
FROM grouped AS g CROSS JOIN controls AS c
ORDER BY g.warehouse_name, g.production_day, g.source_unit"""
    tree = sqlglot.parse_one(sql, read="doris")
    rendered = tree.sql(dialect="doris")
    SqlSafetyValidator().validate(rendered, allowed_sources=SOURCES)
    return rendered


def compile_receipt_production_summary(plan: ReceiptProductionSummaryPlan) -> str:
    start = f"{plan.arrival_start.isoformat()} 00:00:00"
    end = f"{plan.arrival_end.isoformat()} 00:00:00"
    purchase_predicate = {
        "purchase_order_reference": "purchase_order_code <> ''",
        "purchase_type": "inbound_type = '10'",
        "both": "purchase_order_code <> '' AND inbound_type = '10'",
    }[plan.purchase_basis]
    sql = f"""WITH header_source AS (
  SELECT TRIM(CAST(h.cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(h.warehous_code) AS inbound_code,
         COALESCE(TRIM(h.po_order_code), '') AS purchase_order_code,
         COALESCE(TRIM(CAST(h.warehous_type AS STRING)), '') AS inbound_type,
         h.actual_arrival_date,
         COALESCE(TRIM(CAST(h.status AS STRING)), '') AS logical_status,
         COALESCE(TRIM(CAST(h.warehous_status AS STRING)), '') AS receipt_status,
         LOWER(COALESCE(TRIM(CAST(h._fdl_marked_deleted AS STRING)), 'false')) AS deleted
  FROM ywzt_ods.scm_warehous_header AS h
  WHERE h.actual_arrival_date >= '{start}'
    AND h.actual_arrival_date < '{end}'
), eligible_headers AS (
  SELECT cargo_owner, inbound_code, MIN(actual_arrival_date) AS actual_arrival_date,
         COUNT(DISTINCT CONCAT_WS('|', purchase_order_code, inbound_type,
                                  receipt_status, deleted)) AS header_variants
  FROM header_source
  WHERE logical_status = '0'
    AND deleted = 'false'
    AND receipt_status <> '16'
    AND {purchase_predicate}
  GROUP BY cargo_owner, inbound_code
), sku_candidates AS (
  SELECT TRIM(CAST(s.sku_code AS STRING)) AS sku_code,
         COUNT(DISTINCT TRIM(CAST(s.sku_group AS STRING))) AS sku_group_variants,
         MAX(TRIM(CAST(s.sku_group AS STRING))) AS sku_group
  FROM ywzt_ods.master_sku AS s
  WHERE TRIM(CAST(s.sku_code AS STRING)) <> ''
  GROUP BY TRIM(CAST(s.sku_code AS STRING))
), finished_skus AS (
  SELECT sku_code FROM sku_candidates
  WHERE sku_group = '50' AND sku_group_variants = 1
), detail_facts AS (
  SELECT d.cargo_owner_id AS cargo_owner, d.warehous_code AS inbound_code,
         TRIM(CAST(d.id AS STRING)) AS detail_id,
         TRIM(CAST(d.sku_code AS STRING)) AS sku_code,
         d.production_date,
         TRIM(CAST(d.unit AS STRING)) AS source_unit,
         d.received_quantity AS received_quantity,
         h.header_variants
  FROM ywzt_ods.scm_warehous_detail AS d
  JOIN eligible_headers AS h
    ON TRIM(CAST(h.cargo_owner AS STRING)) = TRIM(CAST(d.cargo_owner_id AS STRING))
   AND TRIM(h.inbound_code) = TRIM(d.warehous_code)
  JOIN finished_skus AS s ON s.sku_code = TRIM(CAST(d.sku_code AS STRING))
  WHERE COALESCE(TRIM(CAST(d.status AS STRING)), '0') = '0'
    AND LOWER(COALESCE(TRIM(CAST(d._fdl_marked_deleted AS STRING)), 'false')) = 'false'
    AND d.production_date IS NOT NULL
    AND d.received_quantity IS NOT NULL
    AND TRIM(CAST(d.unit AS STRING)) <> ''
), controls AS (
  SELECT COUNT(*) AS detail_total,
         COUNT(DISTINCT CONCAT_WS('|', TRIM(CAST(cargo_owner AS STRING)),
                                  TRIM(inbound_code), detail_id)) AS detail_distinct,
         MAX(header_variants) AS max_header_variants
  FROM detail_facts
), grouped AS (
  SELECT DATE_FORMAT(production_date, '%Y-%m-%d') AS production_day,
         source_unit,
         COUNT(DISTINCT CONCAT_WS('|', TRIM(CAST(cargo_owner AS STRING)), inbound_code)) AS receipt_count,
         COUNT(*) AS detail_count,
         SUM(received_quantity) AS received_quantity
  FROM detail_facts
  GROUP BY DATE_FORMAT(production_date, '%Y-%m-%d'), source_unit
)
SELECT g.production_day, g.source_unit, g.receipt_count, g.detail_count,
       g.received_quantity, c.detail_total, c.detail_distinct,
       c.max_header_variants
FROM grouped AS g CROSS JOIN controls AS c
ORDER BY g.production_day, g.source_unit"""
    tree = sqlglot.parse_one(sql, read="doris")
    rendered = tree.sql(dialect="doris")
    SqlSafetyValidator().validate(rendered, allowed_sources=SOURCES)
    return rendered
