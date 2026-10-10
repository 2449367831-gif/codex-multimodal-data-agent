"""Reviewed SO07 order-detail route; order facts are independent of shipments."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, Inexact, localcontext
import math
from pathlib import Path
import time as clock
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

from ..sql import SqlSafetyValidator
from ..query_scope import QueryScope
from .dms_points_orders import _codes
from .sales_pages import retrieve_sales_groups
from .sales_order_evidence import (SOURCE_FIELDS, SalesOrderSourceEvidence,
    normalize_order_source_fields, reconcile_order_source_evidence)
from .semantic_types import (SemanticAnswer, SemanticRuntimeError, _display_table,
    _parse_decimal, _parse_source_datetime, _period_label)


TEMPLATE = "sql_templates/sales_order.so07_detail.sql"
SOURCES = frozenset({
    "ywzt_ods.scm_sales_order_header", "ywzt_ods.scm_sales_order_detail", "ywzt_ods.master_wms",
})
ORDER_FIELDS = (
    "cargo_owner", "sales_order_code", "order_created_at", "customer_code", "customer_name",
    "warehouse_code", "warehouse_name", "order_type", "order_status", "detail_id",
    "sku_code", "sku_name", "request_quantity",
) + SOURCE_FIELDS


@dataclass(frozen=True)
class SalesOrderDetailPlan:
    start: date | None
    end: date | None
    order_type: str = "SO07"
    owner: str = "ALL"
    warehouse_codes: tuple[str, ...] = ()
    sku_codes: tuple[str, ...] = ()
    page_limit: int = 500

    def __post_init__(self):
        if (self.start is None) != (self.end is None) or (self.start is not None and
                (type(self.start) is not date or type(self.end) is not date or self.end <= self.start)):
            raise ValueError("Order window must be a nonempty half-open date interval")
        if self.order_type != "SO07":
            raise ValueError("Only the reviewed SO07 order type is executable")
        _codes((self.owner,))
        _codes(self.warehouse_codes)
        _codes(self.sku_codes)
        if type(self.page_limit) is not int or not 1 <= self.page_limit <= 9999:
            raise ValueError("The overflow probe must remain bounded")

    def execution_scope(self):
        return QueryScope(owner=self.owner, warehouse_codes=self.warehouse_codes,
                          sku_codes=self.sku_codes, unit='raw_source')


@dataclass(frozen=True)
class ReconciledSalesOrders:
    rows: tuple[dict[str, Any], ...]
    row_count: int
    order_count: int
    total_request_quantity: Decimal
    missing_customer_count: int
    source_evidence: SalesOrderSourceEvidence


def _literal(value: str) -> exp.Expression:
    return exp.Literal.string(value)


def compile_sales_order_details(plan: SalesOrderDetailPlan, *, project_root: Path | None = None) -> str:
    if not isinstance(plan, SalesOrderDetailPlan):
        raise ValueError("SO07 order-detail plan required")
    root = project_root or Path(__file__).resolve().parents[2]
    tree = sqlglot.parse_one((root / TEMPLATE).read_text(encoding="utf-8"), read="doris")
    bindings = {
        "start_inclusive": _literal(f"{plan.start.isoformat()} 00:00:00") if plan.start else exp.Null(),
        "end_exclusive": _literal(f"{plan.end.isoformat()} 00:00:00") if plan.end else exp.Null(),
        "probe_limit": exp.Literal.number(plan.page_limit + 1),
    }
    placeholders = list(tree.find_all(exp.Placeholder))
    if {node.name for node in placeholders} != set(bindings):
        raise ValueError("SO07 template parameters disagree with the plan")
    for node in placeholders:
        node.replace(bindings[node.name].copy())
    # These values are added as AST predicates so no scope string becomes SQL.
    predicates = []
    if plan.owner != "ALL":
        predicates.append(exp.column("cargo_owner").eq(_literal(plan.owner)))
    if plan.warehouse_codes:
        predicates.append(exp.column("warehouse_code").isin(*[_literal(value) for value in plan.warehouse_codes]))
    if plan.sku_codes:
        predicates.append(exp.column("sku_code").isin(*[_literal(value) for value in plan.sku_codes]))
    if predicates:
        scoped = next(node for node in tree.find_all(exp.CTE) if node.alias == "scoped_facts")
        scoped.set("this", scoped.this.where(exp.and_(*predicates)))
    sql = tree.sql(dialect="doris")
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


def _integer(value: Any) -> int:
    parsed = _parse_decimal(value)
    if parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError("Invalid order control")
    return int(parsed)


def reconcile_sales_order_details(rows: Sequence[Mapping[str, Any]], plan: SalesOrderDetailPlan, *, complete_limit: int | None = None) -> ReconciledSalesOrders:
    try:
        with localcontext() as context:
            context.prec = 64
            context.traps[Inexact] = True
            return _reconcile_sales_order_details(rows, plan, complete_limit=complete_limit)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise SemanticRuntimeError("社团销售订单未通过完整性与对账检查，暂未展示结果", reason="sales_order_quality_failed") from exc


def _reconcile_sales_order_details(rows, plan, *, complete_limit=None):
    if not isinstance(plan, SalesOrderDetailPlan) or not isinstance(rows, (list, tuple)) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("Invalid order result")
    limit = plan.page_limit if complete_limit is None else complete_limit
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError("Invalid complete order result limit")
    if len(rows) > limit:
        raise SemanticRuntimeError("社团销售订单明细已截断，不能作为完整结果", reason="result_truncated")
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime.combine(plan.start, time.min, zone) if plan.start else None
    end = datetime.combine(plan.end, time.min, zone) if plan.end else None
    seen: set[tuple[str, str, str]] = set()
    displayed: list[dict[str, Any]] = []
    total_orders: int | None = None
    total_quantity: Decimal | None = None
    missing_customer_count = 0
    quantity_sum = Decimal(0)
    no_details = all(row.get('detail_id') is None for row in rows)
    try:
        for row in rows:
            if _integer(row.get("source_quality_errors")):
                raise ValueError("Uncertain source deletion state")
            if _integer(row.get("matched_count")) != len(rows):
                raise ValueError("Incomplete order result")
            key = tuple(str(row.get(k) or "").strip() for k in ("cargo_owner", "sales_order_code", "detail_id"))
            missing_detail = row.get("detail_id") is None
            if not all(key[:2]) or (not missing_detail and not key[2]) or key in seen:
                raise ValueError("Order detail key is missing or duplicated")
            if any(not isinstance(row.get(name), str) or row[name] != value
                   for name, value in zip(("cargo_owner", "sales_order_code", "detail_id"), key)
                   if name != 'detail_id' or not missing_detail):
                raise ValueError("Noncanonical order identity")
            seen.add(key)
            if ((plan.owner != 'ALL' and key[0] != plan.owner)
                    or (plan.warehouse_codes and row.get('warehouse_code') not in plan.warehouse_codes)
                    or (plan.sku_codes and row.get('sku_code') not in plan.sku_codes)):
                raise ValueError("Order result is outside requested scope")
            if str(row.get("order_type") or "").strip() != "SO07":
                raise ValueError("Non-SO07 order leaked into result")
            if _integer(row.get("order_variants")) != 1 or (not missing_detail and _integer(row.get("detail_variants")) != 1):
                raise ValueError("Conflicting order facts")
            if _integer(row.get("warehouse_variants")) > 1:
                raise ValueError("Conflicting warehouse mapping")
            created = _parse_source_datetime(row.get("order_created_at"), timezone_name="Asia/Shanghai")
            if start is not None and not start <= created < end:
                raise ValueError("Order date outside requested natural day")
            quantity = Decimal(0) if missing_detail else _parse_decimal(row.get("request_quantity"))
            order_count = _integer(row.get("total_order_count"))
            all_quantity = _parse_decimal(row.get("total_request_quantity"),
                default=Decimal(0) if no_details else None)
            if quantity < 0:
                raise ValueError("Negative request quantity")
            if total_orders is None:
                total_orders, total_quantity = order_count, all_quantity
            if order_count != total_orders or all_quantity != total_quantity:
                raise ValueError("Order controls disagree")
            if (not missing_detail and not str(row.get("sku_code") or "").strip()) or not str(row.get("warehouse_code") or "").strip():
                raise ValueError("Order SKU or warehouse key is missing")
            if missing_detail and any(row.get(k) is not None for k in ("request_quantity", "sku_code", "sku_name", "detail_variants")):
                raise ValueError("Inconsistent missing detail evidence")
            customer_missing = not str(row.get("customer_name") or "").strip()
            missing_customer_count += int(customer_missing)
            values = {key: row.get(key) for key in ORDER_FIELDS}
            values["request_quantity"] = None if missing_detail else format(quantity, "f")
            values["customer_name"] = values["customer_name"] if not customer_missing else None
            values.update(normalize_order_source_fields(row))
            displayed.append(values)
            quantity_sum += quantity
        expected_orders = total_orders or 0
        expected_quantity = total_quantity or Decimal(0)
        if expected_orders != len({(row["cargo_owner"], row["sales_order_code"]) for row in displayed}) or quantity_sum != expected_quantity:
            raise ValueError("Order detail totals do not reconcile")
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise SemanticRuntimeError("社团销售订单未通过完整性与对账检查，暂未展示结果", reason="sales_order_quality_failed") from exc
    evidence = reconcile_order_source_evidence(rows, displayed)
    return ReconciledSalesOrders(tuple(displayed), len(displayed), total_orders or 0,
                                total_quantity or Decimal(0), missing_customer_count, evidence)


def retrieve_sales_order_details(plan: SalesOrderDetailPlan, query, *, query_many=None,
                                 max_rows=100000, max_bytes=64*1024*1024, timeout_seconds=180):
    from .sales_order_references import REFERENCE_FLAGS, compile_sales_order_reference_quality
    if (type(max_rows) is not int or not 1 <= max_rows <= 100000
        or type(max_bytes) is not int or not 1 <= max_bytes <= 64*1024*1024
        or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180):
        raise ValueError('invalid aggregate budget')
    tree = sqlglot.parse_one(compile_sales_order_details(plan), read='doris')
    # The shared pager validates completed-relation counts on every page. Keep
    # matched_count independently for the final order/detail reconciliation.
    tree.select(exp.alias_(exp.Window(this=exp.Count(this=exp.Star())), 'group_count'),
                append=True, copy=False)
    sql = tree.sql(dialect='doris')
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    quality_sql = compile_sales_order_reference_quality(plan)
    deadline = clock.monotonic() + timeout_seconds

    def remaining():
        value = deadline - clock.monotonic()
        if value <= 0:
            raise SemanticRuntimeError('社团销售订单查询超时，暂未展示结果', reason='query_timeout')
        return value

    def check_references():
        remaining()
        controls = query(quality_sql, purpose='SO07 reference integrity')
        remaining()
        try:
            if (not isinstance(controls, (list, tuple)) or len(controls) != 1
                or not isinstance(controls[0], Mapping) or set(controls[0]) != set(REFERENCE_FLAGS)
                or any(_integer(controls[0][name]) != 0 for name in REFERENCE_FLAGS)):
                raise ValueError('unproven order reference integrity')
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            raise SemanticRuntimeError('社团销售订单关联信息缺失或冲突，暂未展示结果',
                                      reason='sales_order_reference_failed') from exc

    check_references()
    rows = retrieve_sales_groups(sql, query, query_many=query_many,
        page_size=min(plan.page_limit, 500), page_cap=500, max_rows=max_rows,
        max_bytes=max_bytes, timeout_seconds=remaining())
    check_references()
    return reconcile_sales_order_details(rows, plan, complete_limit=max_rows)


def retrieve_sales_order_answer(plan: SalesOrderDetailPlan, query, **options):
    result = retrieve_sales_order_details(plan, query, **options)
    fields = ('cargo_owner', 'sales_order_code', 'order_created_at', 'customer_code', 'customer_name',
              'warehouse_code', 'warehouse_name', 'sku_code', 'sku_name', 'request_quantity',
              'quantity_unit_reference', 'sales_unit_reference', 'order_status', 'order_detail_state')
    rows = []
    for source in result.rows:
        row = {name: source[name] for name in fields[:-1]}
        row['order_created_at'] = _parse_source_datetime(
            source['order_created_at'], timezone_name='Asia/Shanghai').strftime('%Y-%m-%d %H:%M:%S')
        for name in ('customer_code', 'warehouse_name', 'sku_name', 'order_status'):
            value = row[name]
            row[name] = None if value is None or not str(value).strip() else str(value)
        row['order_detail_state'] = '缺少范围内有效明细' if source['detail_id'] is None else '存在范围内有效明细'
        rows.append(row)
    period = _period_label(plan.start, plan.end) if plan.start else '全部订单创建时间'
    scope_label = '全部货主' if plan.owner == 'ALL' else '货主 ' + plan.owner
    scope_label += ' · ' + ('指定仓库编码 ' + '、'.join(plan.warehouse_codes) if plan.warehouse_codes else '全部仓库')
    scope_label += ' · ' + ('指定SKU ' + '、'.join(plan.sku_codes) if plan.sku_codes else '全部商品')
    if len(scope_label) > 100:
        scope_label = ('全部货主' if plan.owner == 'ALL' else '指定货主') + ' · ' + (
            f'指定{len(plan.warehouse_codes)}个仓库' if plan.warehouse_codes else '全部仓库') + ' · ' + (
            f'指定{len(plan.sku_codes)}个SKU' if plan.sku_codes else '全部商品')
    table = _display_table(table_id='so07_order_details', title='SO07社团销售订单',
        caption=f'{period} · 按订单创建时间 · 北京时间 · {scope_label}',
        columns=[{'key': name, 'kind': 'number' if name == 'request_quantity' else 'text'} for name in fields],
        rows=rows, summary=[{'label': '订单数', 'value': str(result.order_count), 'unit': '单'}],
        empty_label='查询成功，当前范围没有匹配的SO07社团销售订单')
    evidence = result.source_evidence
    table['notes'] = [
        '请求数量保留来源计量值，附原始数量单位和销售单位代码；不换算为件、箱或重量，不展示跨单位数量合计，也不作为实际出库量。',
        '订单类型为SO07；不依赖出库单，订单状态仅展示。没有范围内有效明细时数量和单位留空，不记为零。'
        f'数量单位代码缺失 {evidence.missing_quantity_unit_reference_count} 行，销售单位代码缺失 {evidence.missing_sales_unit_reference_count} 行；单位含义未确认。',
        f'订单头最近修改：{evidence.source_header_latest_updated or "暂无"}；明细最近修改：{evidence.source_detail_latest_updated or "暂无"}。修改时间缺失：订单 {evidence.missing_header_updated_count} 单、明细 {evidence.missing_detail_updated_count} 行。',
    ]
    table['notes'][-1] += '数仓同步完整性未验证，重复读取一致不代表原子快照。'
    table['complete_reconciled'] = True
    text = (f'已查询到 {result.order_count} 个SO07社团销售订单，原始请求数量与源单位代码见表格。'
            if result.order_count else '查询成功，当前范围没有匹配的SO07社团销售订单。')
    return SemanticAnswer(text, table)
