"""Quantity-free SO07 reference checks; never repair or reattribute facts."""

import sqlglot
from sqlglot import exp

from ..sql import SqlSafetyValidator
from .sales_order_details import SalesOrderDetailPlan


REFERENCE_FLAGS = ('missing_business_reference', 'conflicting_physical_reference')


def compile_sales_order_reference_quality(plan):
    if not isinstance(plan, SalesOrderDetailPlan):
        raise ValueError('SO07 order-detail plan required')
    tree = sqlglot.parse_one("""WITH reference_headers AS (
      SELECT DISTINCT NULLIF(TRIM(id),'') AS header_id,
        NULLIF(TRIM(company_code),'') AS cargo_owner,
        NULLIF(TRIM(sales_order_code),'') AS sales_order_code,
        NULLIF(TRIM(facility_code),'') AS warehouse_code,
        TRIM(order_type) AS order_type, created
      FROM ywzt_ods.scm_sales_order_header
      WHERE (deleted_flag IS NULL OR deleted_flag<>1)
        AND (_fdl_marked_deleted IS NULL OR LOWER(TRIM(_fdl_marked_deleted))<>'true')
    ), reference_details AS (
      SELECT NULLIF(TRIM(sales_order_id),'') AS header_id,
        NULLIF(TRIM(company_code),'') AS cargo_owner,
        NULLIF(TRIM(sales_order_code),'') AS sales_order_code,
        NULLIF(TRIM(facility_code),'') AS warehouse_code,
        NULLIF(TRIM(sku_code),'') AS sku_code
      FROM ywzt_ods.scm_sales_order_detail
      WHERE TRIM(CAST(status AS STRING))='0'
        AND (_fdl_marked_deleted IS NULL OR LOWER(TRIM(_fdl_marked_deleted))<>'true')
    ), reference_candidates AS (
      SELECT h.header_id, h.cargo_owner, h.sales_order_code,
        d.header_id AS detail_header_id, d.cargo_owner AS detail_owner,
        d.sales_order_code AS detail_order
      FROM reference_headers h JOIN reference_details d ON d.header_id=h.header_id
      WHERE h.order_type='SO07'
        AND ((:start_inclusive IS NULL AND :end_exclusive IS NULL)
          OR h.created IS NULL OR (h.created>=:start_inclusive AND h.created<:end_exclusive))
    )
    SELECT COALESCE(MAX(CASE WHEN cargo_owner IS NULL OR sales_order_code IS NULL
        OR detail_owner IS NULL OR detail_order IS NULL
        OR cargo_owner<>detail_owner OR sales_order_code<>detail_order
        THEN 1 ELSE 0 END),0) AS missing_business_reference,
      COALESCE(MAX(CASE WHEN detail_header_id IS NOT NULL
        AND (header_id IS NULL OR detail_header_id<>header_id)
        THEN 1 ELSE 0 END),0) AS conflicting_physical_reference
    FROM reference_candidates LIMIT 1""", read='doris')
    values = {'start_inclusive': f'{plan.start.isoformat()} 00:00:00' if plan.start else None,
              'end_exclusive': f'{plan.end.isoformat()} 00:00:00' if plan.end else None}
    for node in list(tree.find_all(exp.Placeholder)):
        if values[node.name] is None:
            node.replace(exp.Null())
        else:
            node.replace(exp.Literal.string(values[node.name]))
    candidate = next(node for node in tree.find_all(exp.CTE) if node.alias == 'reference_candidates')
    predicates = []
    if plan.owner != 'ALL':
        predicates.append(exp.column('cargo_owner', table='h').eq(exp.Literal.string(plan.owner)))
    # A missing scope key cannot prove that a reachable malformed detail is
    # outside the request. Known unrelated warehouses/SKUs remain excluded.
    for expression, codes in (
        ('COALESCE(d.warehouse_code,h.warehouse_code)', plan.warehouse_codes),
        ('d.sku_code', plan.sku_codes),
    ):
        if codes:
            column = sqlglot.parse_one(expression, read='doris')
            predicates.append(exp.or_(column.copy().is_(exp.Null()),
                column.isin(*[exp.Literal.string(code) for code in codes])))
    if predicates:
        candidate.set('this', candidate.this.where(exp.and_(*predicates)))
    # Each branch retains an equality join key for Doris. Duplicate reachability
    # is harmless: final controls are boolean MAX flags, never quantity sums.
    branches = [candidate.this.copy()]
    for predicate in (
        'd.cargo_owner=h.cargo_owner AND d.sales_order_code=h.sales_order_code',
        'd.cargo_owner IS NULL AND d.sales_order_code=h.sales_order_code',
        'd.sales_order_code IS NULL AND d.cargo_owner=h.cargo_owner',
    ):
        branch = candidate.this.copy()
        branch.args['joins'][0].set('on', sqlglot.parse_one(predicate, read='doris'))
        branches.append(branch)
    relation = branches[0]
    for branch in branches[1:]:
        relation = relation.union(branch, distinct=False)
    candidate.set('this', relation)
    sql = tree.sql(dialect='doris')
    SqlSafetyValidator().validate(sql, allowed_sources=frozenset({
        'ywzt_ods.scm_sales_order_header', 'ywzt_ods.scm_sales_order_detail'}))
    return sql
