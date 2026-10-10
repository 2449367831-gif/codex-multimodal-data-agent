"""DMS order-header aggregation with lossless, independently checked enrichment."""

from collections import Counter
from dataclasses import dataclass
from datetime import date

import sqlglot
from sqlglot import exp

from .dms_manager_lookup import canonical_manager_id, compile_manager_lookup, reconcile_manager_lookup
from .dms_points_orders import _codes, _count
from .semantic_types import SemanticRuntimeError, _parse_source_datetime


@dataclass(frozen=True, slots=True)
class DmsManagerOrderPlan:
    start: date | None = None
    end: date | None = None
    customer_codes: tuple[str, ...] = ()
    order_codes: tuple[str, ...] = ()

    def __post_init__(self):
        if (self.start is None) != (self.end is None):
            raise ValueError('both creation bounds required')
        if self.start is not None and (type(self.start) is not date or type(self.end) is not date or self.end <= self.start):
            raise ValueError('invalid creation interval')
        _codes(self.customer_codes)
        _codes(self.order_codes)


GROUP_FIELDS = ('order_date', 'province', 'manager_key', 'manager_state', 'customer_code', 'customer_name')
ORDER_FIELDS = ('sales_order_code', 'customer_code', 'customer_name', 'created_time', 'updated_time',
                'province', 'manager_reference', 'employee_id', 'manager_name', 'manager_state',
                'variant_count', 'source_rows', 'source_updated', 'order_date', 'manager_key')
CONTROLS = ('total_order_count', 'group_count', 'missing_manager_order_count', 'missing_province_order_count',
            'missing_customer_code_count', 'missing_customer_name_count', 'missing_updated_count',
            'blank_order_count', 'missing_created_count', 'version_conflict_count', 'invalid_delete_count')
QUALITY = ('blank_order_count', 'missing_created_count', 'version_conflict_count', 'invalid_delete_count')


def compile_manager_orders(plan):
    if not isinstance(plan, DmsManagerOrderPlan):
        raise ValueError('manager order plan required')
    def selected(column, values):
        return 'TRUE' if not values else column + ' IN (' + ','.join(exp.Literal.string(value).sql(dialect='doris') for value in values) + ')'
    scope = selected('customer_code',plan.customer_codes) + ' AND ' + selected('sales_order_code',plan.order_codes)
    interval = 'TRUE' if plan.start is None else f"created_time >= '{plan.start.isoformat()} 00:00:00' AND created_time < '{plan.end.isoformat()} 00:00:00'"
    eligible = f"COALESCE(deleted_flag,0)<>1 AND cdc_deleted NOT IN ('true','1') AND ({scope}) AND (created_time IS NULL OR ({interval}))"
    # Reuse the tested employee CTEs with references derived from the complete
    # scoped order fact. No 200-reference sample or literal batch limits the facts.
    manager = sqlglot.parse_one(compile_manager_lookup(()),read='doris').args['with_'].expressions
    manager[0].set('this',sqlglot.parse_one('SELECT ROW_NUMBER() OVER(ORDER BY manager_reference) AS reference_ordinal,manager_reference FROM (SELECT DISTINCT manager_reference FROM orders) order_references',read='doris'))
    manager_ctes = ',\n'.join(cte.sql(dialect='doris') for cte in manager)
    nulls = ','.join('CAST(NULL AS ' + ('DATETIME' if key in {'created_time','updated_time','source_updated'} else 'BIGINT' if key in {'variant_count','source_rows'} else 'STRING') + ')' for key in ORDER_FIELDS)
    return f"""WITH order_source AS (
 SELECT NULLIF(TRIM(o.sales_order_code),'') AS sales_order_code,
   NULLIF(TRIM(o.customer_code),'') AS customer_code,NULLIF(TRIM(o.customer_name),'') AS customer_name,
   o.created_time,o.updated_time,o.version,o.id,o.deleted_flag,
   LOWER(TRIM(COALESCE(o._fdl_marked_deleted,''))) AS cdc_deleted,
   o.owner_manager AS manager_reference,NULLIF(TRIM(o.province_department_name),'') AS province
 FROM dms_ods.t_sales_order o
), order_ranked AS (
 SELECT *,DENSE_RANK() OVER(PARTITION BY sales_order_code ORDER BY updated_time DESC NULLS LAST,version DESC NULLS LAST,id DESC) AS order_version_rank
 FROM order_source WHERE sales_order_code IS NOT NULL
), order_versions AS (
 SELECT DISTINCT sales_order_code,customer_code,customer_name,created_time,updated_time,
   deleted_flag,cdc_deleted,manager_reference,province FROM order_ranked WHERE order_version_rank=1
), order_version_controls AS (
 SELECT sales_order_code,COUNT(*) AS top_variants FROM order_versions GROUP BY sales_order_code
), order_candidates AS (
 SELECT * FROM order_versions WHERE {eligible}
), orders AS (
 SELECT sales_order_code,MIN(customer_code) AS customer_code,MIN(customer_name) AS customer_name,
   MIN(created_time) AS created_time,MIN(updated_time) AS updated_time,
   MIN(province) AS province,MIN(manager_reference) AS manager_reference
 FROM order_candidates WHERE created_time IS NOT NULL AND COALESCE(deleted_flag,0)=0 AND cdc_deleted IN ('','false','0')
 GROUP BY sales_order_code
), {manager_ctes}, enriched AS (
 SELECT o.sales_order_code,o.customer_code,o.customer_name,o.created_time,o.updated_time,
   o.province,o.manager_reference,m.employee_id,m.manager_name,m.match_state AS manager_state,
   m.variant_count,m.source_rows,m.source_updated,DATE_FORMAT(o.created_time,'%Y-%m-%d') AS order_date,
   COALESCE(m.employee_id,NULLIF(TRIM(o.manager_reference),'')) AS manager_key
 FROM orders o LEFT JOIN resolved m ON o.manager_reference <=> m.manager_reference
), order_quality AS (
 SELECT (SELECT COUNT(*) FROM order_source WHERE sales_order_code IS NULL AND {eligible}) AS blank_order_count,
   (SELECT COUNT(DISTINCT sales_order_code) FROM order_candidates WHERE created_time IS NULL) AS missing_created_count,
   (SELECT COUNT(DISTINCT c.sales_order_code) FROM order_candidates c JOIN order_version_controls v ON c.sales_order_code=v.sales_order_code WHERE v.top_variants>1) AS version_conflict_count,
   (SELECT COUNT(DISTINCT sales_order_code) FROM order_candidates WHERE cdc_deleted NOT IN ('','false','0') OR COALESCE(deleted_flag,0) NOT IN (0,1)) AS invalid_delete_count
), order_controls AS (
 SELECT (SELECT COUNT(*) FROM orders) AS total_order_count,1+(SELECT COUNT(*) FROM orders) AS group_count,
   COALESCE(SUM(CASE WHEN manager_state<>'matched' THEN 1 ELSE 0 END),0) AS missing_manager_order_count,
   COALESCE(SUM(CASE WHEN province IS NULL THEN 1 ELSE 0 END),0) AS missing_province_order_count,
   COALESCE(SUM(CASE WHEN customer_code IS NULL THEN 1 ELSE 0 END),0) AS missing_customer_code_count,
   COALESCE(SUM(CASE WHEN customer_name IS NULL THEN 1 ELSE 0 END),0) AS missing_customer_name_count,
   COALESCE(SUM(CASE WHEN updated_time IS NULL THEN 1 ELSE 0 END),0) AS missing_updated_count,
   MAX(updated_time) AS source_latest_updated FROM enriched
), report AS (
 SELECT 'order' AS row_kind,e.*,COUNT(*) OVER(PARTITION BY {','.join(GROUP_FIELDS)}) AS group_order_count FROM enriched e
 UNION ALL SELECT 'control',{nulls},0
)
SELECT r.*,c.*,q.* FROM report r CROSS JOIN order_controls c CROSS JOIN order_quality q
ORDER BY r.row_kind,r.sales_order_code LIMIT 501"""


def _text(value):
    if value is not None and (not isinstance(value,str) or not value or value != value.strip(' ')):
        raise ValueError('invalid order text')
    return value


def _time(value):
    return _parse_source_datetime(value,timezone_name='Asia/Shanghai') if value is not None else None


@dataclass(frozen=True)
class DmsManagerOrderResult:
    orders: list[dict]
    groups: list[dict]
    order_count: int
    missing_manager_order_count: int
    missing_province_order_count: int
    missing_customer_code_count: int
    missing_customer_name_count: int
    missing_updated_count: int
    source_latest_updated: str | None


def reconcile_manager_orders(rows,plan,*,complete_limit=100000):
    if type(complete_limit) is not int or not 1<=complete_limit<=100000 or not rows or len(rows)>complete_limit:
        raise ValueError('missing or truncated DMS controls')
    controls={key:_count(rows[0][key]) for key in CONTROLS}
    if any(controls[key] for key in QUALITY):
        raise ValueError('DMS order quality failed')
    if controls['group_count']!=len(rows) or len(rows)!=1+controls['total_order_count']:
        raise ValueError('incomplete DMS manager order report')
    latest=_time(rows[0]['source_latest_updated'])
    orders,groups,counts,seen,manager_observations=[],{},Counter(),set(),{}
    actual_controls=Counter()
    observed_latest=None
    control_rows=0
    for row in rows:
        if any(_count(row[key])!=controls[key] for key in CONTROLS) or _time(row['source_latest_updated'])!=latest:
            raise ValueError('inconsistent DMS manager controls')
        if row['row_kind']=='control':
            control_rows+=1
            if any(row[key] is not None for key in ORDER_FIELDS) or _count(row['group_order_count'])!=0:
                raise ValueError('invalid DMS control row')
            continue
        if row['row_kind']!='order':
            raise ValueError('unknown DMS report row')
        code=_text(row['sales_order_code'])
        customer,name,province=(_text(row[key]) for key in ('customer_code','customer_name','province'))
        if not code or code in seen or (plan.customer_codes and customer not in plan.customer_codes) or (plan.order_codes and code not in plan.order_codes):
            raise ValueError('duplicate or out of scope order')
        seen.add(code)
        created,updated=_time(row['created_time']),_time(row['updated_time'])
        if created is None or row['order_date']!=created.strftime('%Y-%m-%d') or (plan.start is not None and not plan.start<=created.date()<plan.end):
            raise ValueError('invalid order creation date')
        if updated is not None:
            observed_latest=max(observed_latest,updated) if observed_latest is not None else updated
        raw=row['manager_reference']
        observation={key:row[key] for key in ('manager_reference','employee_id','manager_name','variant_count','source_rows','source_updated')}
        observation.update(reference_ordinal=1,group_count=1,match_state=row['manager_state'])
        checked=reconcile_manager_lookup([observation],(raw,))[0]
        if raw in manager_observations and manager_observations[raw]!=checked:
            raise ValueError('employee attributes changed between orders')
        manager_observations[raw]=checked
        manager_key=checked['employee_id'] or ((raw.strip(' ') or None) if raw is not None else None)
        if row['manager_key']!=manager_key:
            raise ValueError('manager grouping identity changed')
        key=tuple(row[field] for field in GROUP_FIELDS)
        counts[key]+=1
        declared=_count(row['group_order_count'])
        group={field:row[field] for field in GROUP_FIELDS}
        group.update(manager_name=checked['manager_name'],order_count=declared,
                     missing_manager_order_count=declared if checked['match_state']!='matched' else 0)
        if key in groups and groups[key]!=group:
            raise ValueError('inconsistent manager group')
        groups[key]=group
        actual_controls.update({'missing_manager_order_count':checked['match_state']!='matched',
            'missing_province_order_count':province is None,'missing_customer_code_count':customer is None,
            'missing_customer_name_count':name is None,'missing_updated_count':updated is None})
        orders.append({'sales_order_code':code,'customer_code':customer,'customer_name':name,
            'created_time':created.strftime('%Y-%m-%d %H:%M:%S'),'province':province,
            'manager_reference':raw,'employee_id':checked['employee_id'],'manager_name':checked['manager_name'],
            'manager_state':checked['match_state'],'manager_key':manager_key,'order_date':row['order_date']})
    if control_rows!=1 or len(orders)!=controls['total_order_count'] or any(groups[key]['order_count']!=value for key,value in counts.items()):
        raise ValueError('order detail and grouped counts do not reconcile')
    if observed_latest!=latest or any(controls[key]!=value for key,value in actual_controls.items()):
        raise ValueError('missing fields or freshness do not reconcile')
    if not orders and any(controls[key] for key in CONTROLS if key not in {'group_count'}):
        raise ValueError('empty report contains nonzero controls')
    sorted_groups=[groups[key] for key in sorted(groups,key=lambda values:tuple((value is not None,value or '') for value in values))]
    return DmsManagerOrderResult(orders,sorted_groups,len(orders),*(controls[key] for key in (
        'missing_manager_order_count','missing_province_order_count','missing_customer_code_count',
        'missing_customer_name_count','missing_updated_count')),latest.strftime('%Y-%m-%d %H:%M:%S') if latest else None)


def _retrieve_report(plan,query,compiler,reconciler,*,query_many=None,page_size=500,page_cap=500,max_rows=100000,max_bytes=64*1024*1024,timeout_seconds=180):
    from .sales_pages import retrieve_sales_groups
    purpose_label='DMS manager order report: bounded complete page'
    def checked(rows):
        if any(_count(row[key]) for row in rows for key in QUALITY):
            raise SemanticRuntimeError('DMS订单标识、时间或当前版本存在异常，暂未展示',reason='dms_manager_order_quality_failed')
        return rows
    def read(sql,*,purpose):
        return checked(query(sql,purpose=purpose_label))
    def read_many(statements,*,purpose,timeout_seconds):
        return [checked(rows) for rows in query_many(statements,purpose=purpose_label,timeout_seconds=timeout_seconds)]
    try:
        rows=retrieve_sales_groups(compiler(plan),read,query_many=read_many if query_many is not None else None,page_size=page_size,page_cap=page_cap,max_rows=max_rows,max_bytes=max_bytes,timeout_seconds=timeout_seconds)
        return reconciler(rows,plan,complete_limit=max_rows)
    except SemanticRuntimeError as exc:
        if exc.reason!='sales_page_invalid':
            raise
        raise SemanticRuntimeError('DMS订单结果存在缺页或变化，暂未展示',reason='dms_manager_order_page_invalid') from exc
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError('DMS订单分组与明细未通过对账，暂未展示',reason='dms_manager_order_reconciliation_failed') from exc


def retrieve_manager_orders(plan,query,**budgets):
    return _retrieve_report(plan,query,compile_manager_orders,reconcile_manager_orders,**budgets)


GROUP_RECORD_FIELDS = GROUP_FIELDS + ('manager_name','variant_count','source_rows','source_updated',
                                     'order_count','group_missing_updated','group_latest_updated')


def compile_manager_groups(plan):
    base=sqlglot.parse_one(compile_manager_orders(plan),read='doris')
    ctes=','.join(cte.sql(dialect='doris') for cte in base.args['with_'].expressions if cte.alias_or_name!='report')
    attributes=GROUP_FIELDS+('manager_name','variant_count','source_rows','source_updated')
    nulls=','.join('CAST(NULL AS '+('BIGINT' if field in {'variant_count','source_rows','order_count','group_missing_updated'} else 'DATETIME' if field in {'source_updated','group_latest_updated'} else 'STRING')+')' for field in GROUP_RECORD_FIELDS)
    control_projection=','.join('c.'+key for key in CONTROLS if key not in QUALITY and key!='group_count')
    return f"""WITH {ctes}, manager_groups AS (
 SELECT {','.join(attributes)},COUNT(*) AS order_count,
   SUM(CASE WHEN updated_time IS NULL THEN 1 ELSE 0 END) AS group_missing_updated,
   MAX(updated_time) AS group_latest_updated
 FROM enriched GROUP BY {','.join(attributes)}
), group_report AS (
 SELECT 'group' AS row_kind,g.* FROM manager_groups g
 UNION ALL SELECT 'control',{nulls}
)
SELECT g.*,{control_projection},1+(SELECT COUNT(*) FROM manager_groups) AS group_count,
 c.source_latest_updated,q.* FROM group_report g CROSS JOIN order_controls c CROSS JOIN order_quality q
ORDER BY row_kind,{','.join(GROUP_FIELDS)} LIMIT 501"""


@dataclass(frozen=True)
class DmsManagerGroupResult:
    groups: list[dict]
    order_count: int
    missing_manager_order_count: int
    missing_province_order_count: int
    missing_customer_code_count: int
    missing_customer_name_count: int
    missing_updated_count: int
    source_latest_updated: str | None


def reconcile_manager_groups(rows,plan,*,complete_limit=100000):
    if type(complete_limit) is not int or not 1<=complete_limit<=100000 or not rows or len(rows)>complete_limit:
        raise ValueError('missing or truncated grouped controls')
    control={key:_count(rows[0][key]) for key in CONTROLS}
    if any(control[key] for key in QUALITY):
        raise ValueError('DMS order quality failed')
    if control['group_count']!=len(rows):
        raise ValueError('incomplete grouped report')
    latest=_time(rows[0]['source_latest_updated'])
    groups,seen,counts=[],set(),Counter()
    control_rows=0
    observed_latest=None
    manager_observations={}
    for row in rows:
        if any(_count(row[key])!=control[key] for key in CONTROLS) or _time(row['source_latest_updated'])!=latest:
            raise ValueError('inconsistent group controls')
        if row['row_kind']=='control':
            control_rows+=1
            if any(row[key] is not None for key in GROUP_RECORD_FIELDS):
                raise ValueError('invalid group control row')
            continue
        if row['row_kind']!='group':
            raise ValueError('invalid group kind')
        key=tuple(_text(row[field]) for field in GROUP_FIELDS)
        day,province,manager_key,state,customer,name=key
        if day is None or date.fromisoformat(day).isoformat()!=day or (plan.start is not None and not plan.start<=date.fromisoformat(day)<plan.end):
            raise ValueError('invalid grouped creation date')
        if key in seen or (plan.customer_codes and customer not in plan.customer_codes):
            raise ValueError('duplicate or out of scope group')
        seen.add(key)
        count,missing_updated=_count(row['order_count']),_count(row['group_missing_updated'])
        if not count or missing_updated>count:
            raise ValueError('invalid grouped order counts')
        observation={field:row[field] for field in ('manager_name','variant_count','source_rows','source_updated')}
        observation.update(manager_reference=manager_key,employee_id=canonical_manager_id(manager_key),match_state=state,reference_ordinal=1,group_count=1)
        checked=reconcile_manager_lookup([observation],(manager_key,))[0]
        if manager_key in manager_observations and manager_observations[manager_key]!=checked:
            raise ValueError('manager attributes changed between groups')
        manager_observations[manager_key]=checked
        updated=_time(row['group_latest_updated'])
        if (updated is None)!=(missing_updated==count):
            raise ValueError('invalid group modification time')
        if updated is not None:
            observed_latest=max(observed_latest,updated) if observed_latest is not None else updated
        counts.update({'total_order_count':count,'missing_manager_order_count':count if state!='matched' else 0,
                       'missing_province_order_count':count if province is None else 0,
                       'missing_customer_code_count':count if customer is None else 0,
                       'missing_customer_name_count':count if name is None else 0,'missing_updated_count':missing_updated})
        group=dict(zip(GROUP_FIELDS,key))
        group.update(manager_name=checked['manager_name'],order_count=count,missing_manager_order_count=count if state!='matched' else 0)
        groups.append(group)
    checked_counts=('total_order_count','missing_manager_order_count','missing_province_order_count',
                    'missing_customer_code_count','missing_customer_name_count','missing_updated_count')
    if control_rows!=1 or any(counts[key]!=control[key] for key in checked_counts) or observed_latest!=latest:
        raise ValueError('group and independent order controls do not reconcile')
    groups.sort(key=lambda row:tuple((row[key] is not None,row[key] or '') for key in GROUP_FIELDS))
    return DmsManagerGroupResult(groups,*(control[key] for key in checked_counts),latest.strftime('%Y-%m-%d %H:%M:%S') if latest else None)


def retrieve_manager_groups(plan,query,**budgets):
    return _retrieve_report(plan,query,compile_manager_groups,reconcile_manager_groups,**budgets)
