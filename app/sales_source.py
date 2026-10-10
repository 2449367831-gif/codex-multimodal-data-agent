"""Explicit compilers for the requester-approved hybrid sales source.

This never rewrites model SQL. Natural-language planning receives the same
released templates; deterministic rule forms and turnover share this binder.
"""
from datetime import date, datetime, time, timedelta
from functools import wraps
from pathlib import Path

import sqlglot
from sqlglot import exp

from .query_scope import QueryScope

FACTS_TEMPLATE = "sql_templates/sales.business_quantity.facts.sql"
QUALITY_COLUMN = "__sales_join_conflicts"
BASIS_LABEL = "社区团购平台按订单请求量、创建日期减一天；其余渠道按实际发货量"


def _datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, (date, datetime)):
        raise ValueError("sales window requires dates")
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if value.tzinfo is not None:
        from zoneinfo import ZoneInfo
        value = value.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return value


def _literal(value):
    return exp.Literal.string(str(value)).sql(dialect="doris")


def sales_ctes(start, end, *, scope=QueryScope(), root=None):
    start, end = _datetime(start), _datetime(end)
    if start >= end:
        raise ValueError("sales window must be nonempty and half-open")
    if root is None:
        from .knowledge_activation import active_snapshot
        root, _ = active_snapshot(Path(__file__).resolve().parents[1])
    tree = sqlglot.parse_one((root / FACTS_TEMPLATE).read_text(encoding="utf-8"), read="doris")
    bindings = dict(start_inclusive=start, end_exclusive=end,
                    community_start=start + timedelta(days=1), community_end=end + timedelta(days=1))
    for node in list(tree.find_all(exp.Placeholder)):
        node.replace(exp.Literal.string(bindings[node.name].isoformat(sep=" ")))
    for select in (tree.this, tree.expression):
        detail = "i" if select is tree.this else "d"
        if scope.owner != "ALL":
            select.where(f"h.company_code = {_literal(scope.owner)}", append=True, copy=False)
        if scope.warehouse_codes:
            select.where("h.facility_code IN (" + ",".join(map(_literal, scope.warehouse_codes)) + ")", append=True, copy=False)
        if scope.sku_codes:
            select.where(f"{detail}.sku_code IN (" + ",".join(map(_literal, scope.sku_codes)) + ")", append=True, copy=False)
        if scope.finished_goods:
            select.where("m.sku_group = '50'", append=True, copy=False)
        if scope.product_terms:
            select.where("(" + " OR ".join(f"m.sku_name LIKE {_literal('%'+term+'%')}" for term in scope.product_terms) + ")", append=True, copy=False)
        if scope.channel != "ALL":
            channels = {"线上": "s.type = 'Z004'", "线下": "s.type = 'Z001'", "其他": "(s.type NOT IN ('Z004','Z001') OR s.type IS NULL)"}
            if scope.channel not in channels:
                raise ValueError("unsupported explicit sales channel")
            select.where(channels[scope.channel], append=True, copy=False)
    # Check only keys contributing to this request. Keep the supplied JOINs;
    # do not pick MAX or silently discard conflicting header/master records.
    checks = []
    for table, key, fact_key, condition, source_filter in (
        ("master_store", "code", "store_code", "1=1", "1=1"),
        ("master_sku", "sku_code", "sku_code", "1=1", "1=1"),
        ("master_wms", "wms_code", "wms_code", "1=1", "1=1"),
        ("scm_sales_shipment_header", "outbound_code", "document_code", "1=1", "source_kind='shipment'"),
        ("scm_sales_order_header", "sales_order_code", "sales_order_code", "1=1", "source_kind='shipment'"),
        ("scm_sales_order_header", "sales_order_code", "sales_order_code", "COALESCE(_fdl_marked_deleted,'false')='false'", "source_kind='community_order'"),
    ):
        checks.append(f"SELECT COUNT(*) AS n FROM (SELECT {key} FROM ywzt_ods.{table} WHERE {condition} AND {key} IN (SELECT {fact_key} FROM sales_current WHERE {source_filter}) GROUP BY {key} HAVING COUNT(*) > 1) AS conflicting_keys")
    query = sqlglot.parse_one("WITH sales_current AS (" + tree.sql(dialect="doris") + "), bs_quality AS (SELECT COALESCE(SUM(n),0) AS conflicts FROM (" + " UNION ALL ".join(checks) + ") AS issues) SELECT 1", read="doris")
    return list(query.args["with_"].expressions)


def bind_business_sales(sql, start, end, *, scope=QueryScope(), root=None):
    query = sqlglot.parse_one(sql, read="doris")
    if not any(t.name == "sales_current" and not t.db for t in query.find_all(exp.Table)):
        raise ValueError("compiler must explicitly select sales_current")
    prefix = sales_ctes(start, end, scope=scope, root=root)
    existing = list(query.args["with_"].expressions) if query.args.get("with_") else []
    if {c.alias for c in prefix} & {c.alias for c in existing}:
        raise ValueError("sales source CTE name collision")
    query.set("with_", exp.With(expressions=prefix + existing))
    # Only result projections, never selects inside CTEs/subqueries.
    def branches(node):
        if isinstance(node, exp.Union):
            yield from branches(node.this)
            yield from branches(node.expression)
        else:
            yield node
    for branch in branches(query):
        branch.select(f"(SELECT conflicts FROM bs_quality) AS {QUALITY_COLUMN}", append=True, copy=False)
    return query.sql(dialect="doris")


def compile_with_business_sales(builder):
    @wraps(builder)
    def compiled(self, start, end, *args, **kwargs):
        return bind_business_sales(builder(self, start, end, *args, **kwargs), start, end,
                                   scope=self.execution.scope)
    return compiled
