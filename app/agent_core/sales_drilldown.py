"""One bounded, reconciled fact snapshot for ranked sales and immutable drilldown."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import sqlglot
from sqlglot import exp

from ..query_scope import QueryScope, apply_scope
from .sales_aggregation import SalesAggregation, compile_sales_aggregation
from .sales_pages import retrieve_sales_groups
from .semantic_types import SemanticRuntimeError, _parse_decimal, _parse_source_datetime
from .handlers.sales_aggregate import _integer


# Within the shared 10,000-row page ceiling; total rows, bytes, and deadlines
# remain enforced by retrieve_sales_groups for both complete consistency reads.
DRILLDOWN_PAGE_SIZE = 5000


@dataclass(frozen=True, slots=True)
class SalesDrilldownPlan:
    start: date
    end: date
    top_n: int = 20
    scope: QueryScope = field(default_factory=lambda: QueryScope(finished_goods=True))

    def __post_init__(self):
        if type(self.start) is not date or type(self.end) is not date or self.end<=self.start:
            raise ValueError('nonempty natural-day interval required')
        if type(self.top_n) is not int or not 1<=self.top_n<=100:
            raise ValueError('invalid sales ranking size')
        if (not isinstance(self.scope,QueryScope) or not self.scope.finished_goods
            or self.scope.channel!='ALL' or self.scope.unit!='EA'):
            raise ValueError('unsupported ranked sales scope')
        if self.scope.finished_goods_exceptions and set(self.scope.finished_goods_exceptions)!={'5101010010008','5101010010009'}:
            raise ValueError('unapproved finished-goods exception set')


DETAIL_FIELDS=('sku_rank','sku_code','sku_name','sku_quantity','sku_outbound_count',
               'cargo_owner','warehouse_code','warehouse_name','customer_code',
               'outbound_code','ship_at','quantity')
COUNT_CONTROLS=('total_outbound_count','total_sku_count','selected_outbound_count',
                'selected_sku_count','detail_count','group_count','header_conflict_count',
                'null_quantity_count','negative_quantity_count','blank_key_count',
                'master_conflict_count','category_conflict_count','warehouse_conflict_count')
QUALITY=COUNT_CONTROLS[6:]


def compile_sales_drilldown(plan):
    if not isinstance(plan,SalesDrilldownPlan):
        raise ValueError('sales drilldown plan required')
    base=sqlglot.parse_one(compile_sales_aggregation(SalesAggregation(('sku',)),start=plan.start,end=plan.end),read='doris')
    ctes={cte.alias_or_name:cte for cte in base.args['with_'].expressions}
    # Customer identity is the shipment-header code, not an unconfirmed master join.
    # Include it in duplicate-header checks before reducing to the document grain.
    for name,projection in (
        ('header_variants',"NULLIF(TRIM(h.store_code),'') AS customer_code"),
        ('headers','MIN(customer_code) AS customer_code'),
        ('facts','h.customer_code'),
    ):
        ctes[name].this.select(sqlglot.parse_one(projection,read='doris'),append=True,copy=False)
    scoped=sqlglot.parse_one(apply_scope(base.sql(dialect='doris'),plan.scope,legacy_owner='HJXH'),read='doris')
    cte_sql=',\n'.join(cte.sql(dialect='doris') for cte in scoped.args['with_'].expressions)
    exceptions=','.join(exp.Literal.string(code).sql(dialect='doris') for code in plan.scope.finished_goods_exceptions)
    exception_filter=f"AND r.sku_code NOT IN ({exceptions})" if exceptions else ''
    # Let UNION infer types from the facts; a chosen decimal scale could round EA.
    nulls=','.join('NULL' for _ in DETAIL_FIELDS)
    return f"""WITH {cte_sql}, ranked_skus AS (
 SELECT sku_code,quantity AS sku_quantity,outbound_count AS sku_outbound_count,
   ROW_NUMBER() OVER(ORDER BY quantity DESC,sku_code ASC) AS sku_rank
 FROM summary
), selected_skus AS (
 SELECT * FROM ranked_skus WHERE sku_rank<={plan.top_n}
), sku_names AS (
 SELECT TRIM(m.sku_code) AS sku_code,MIN(NULLIF(TRIM(m.sku_name),'')) AS sku_name,
   COUNT(DISTINCT COALESCE(NULLIF(TRIM(m.sku_name),''),'<missing>')) AS name_variants,
   MIN(NULLIF(TRIM(m.sku_group),'')) AS sku_group,
   COUNT(DISTINCT COALESCE(NULLIF(TRIM(m.sku_group),''),'<missing>')) AS group_variants
 FROM ywzt_ods.master_sku m
 WHERE EXISTS(SELECT 1 FROM selected_skus r WHERE r.sku_code=TRIM(m.sku_code))
 GROUP BY TRIM(m.sku_code)
), selected_facts AS (
 SELECT f.* FROM facts f JOIN selected_skus r ON r.sku_code=f.sku_code
), selected_documents AS (
 SELECT DISTINCT cargo_owner,outbound_code FROM selected_facts
), selected_controls AS (
 SELECT COALESCE(SUM(quantity),0) AS selected_quantity,COUNT(*) AS detail_count FROM selected_facts
), dimension_controls AS (
 SELECT (SELECT COUNT(*) FROM selected_skus r LEFT JOIN sku_names m ON m.sku_code=r.sku_code
   WHERE COALESCE(m.name_variants,0)>1) AS master_conflict_count,
   (SELECT COUNT(*) FROM selected_skus r LEFT JOIN sku_names m ON m.sku_code=r.sku_code
   WHERE (COALESCE(m.group_variants,0)<>1 OR COALESCE(m.sku_group,'')<>'50') {exception_filter}) AS category_conflict_count,
   (SELECT COUNT(*) FROM selected_facts f JOIN wms_names w ON w.cargo_owner=f.cargo_owner AND w.warehouse_code=f.warehouse_code
   WHERE w.warehouse_name_variants>1) AS warehouse_conflict_count
), report AS (
 SELECT 'detail' AS row_kind,r.sku_rank,f.sku_code,COALESCE(m.sku_name,f.sku_code) AS sku_name,
   r.sku_quantity,r.sku_outbound_count,f.cargo_owner,f.warehouse_code,
   COALESCE(w.warehouse_name,f.warehouse_code) AS warehouse_name,f.customer_code,
   f.outbound_code,f.ship_at,f.quantity
 FROM selected_facts f JOIN selected_skus r ON r.sku_code=f.sku_code
 LEFT JOIN sku_names m ON m.sku_code=f.sku_code
 LEFT JOIN wms_names w ON w.cargo_owner=f.cargo_owner AND w.warehouse_code=f.warehouse_code
 UNION ALL SELECT 'control',{nulls}
)
SELECT r.*,c.total_quantity,c.latest_shipment_at,c.null_quantity_count,c.negative_quantity_count,c.blank_key_count,
 hc.header_conflict_count,sc.*,dc.*,
 (SELECT COUNT(*) FROM all_documents) AS total_outbound_count,
 (SELECT COUNT(*) FROM summary) AS total_sku_count,
 (SELECT COUNT(*) FROM selected_documents) AS selected_outbound_count,
 (SELECT COUNT(*) FROM selected_skus) AS selected_sku_count,sc.detail_count+1 AS group_count
FROM report r CROSS JOIN controls c CROSS JOIN header_controls hc
 CROSS JOIN selected_controls sc CROSS JOIN dimension_controls dc
ORDER BY row_kind,sku_rank,cargo_owner,outbound_code LIMIT 501"""


@dataclass(frozen=True, slots=True)
class RankedSku:
    rank: int
    sku_code: str
    sku_name: str
    quantity: Decimal
    outbound_count: int


@dataclass(frozen=True, slots=True)
class SalesDrilldownRow:
    sku_code: str
    cargo_owner: str
    warehouse_code: str
    warehouse_name: str
    customer_code: str | None
    outbound_code: str
    ship_at: datetime
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class SalesDrilldownSnapshot:
    plan: SalesDrilldownPlan
    ranking: tuple[RankedSku,...]
    details: tuple[SalesDrilldownRow,...]
    total_quantity: Decimal
    total_outbound_count: int
    selected_quantity: Decimal
    selected_outbound_count: int
    latest_shipment_at: datetime | None

    def drilldown(self,sku_code):
        if sku_code not in {row.sku_code for row in self.ranking}:
            raise KeyError('SKU is not in this ranking snapshot')
        return tuple(row for row in self.details if row.sku_code==sku_code)


def _text(value,*,optional=False):
    if value is None and optional:
        return None
    if not isinstance(value,str) or not value or value!=value.strip(' '):
        raise ValueError('missing or malformed business key')
    return value


def _time(value):
    return _parse_source_datetime(value,timezone_name='Asia/Shanghai') if value is not None else None


def reconcile_sales_drilldown(rows,plan,*,complete_limit=100000,expected_sku=None):
    if type(complete_limit) is not int or not 1<=complete_limit<=100000 or not rows or len(rows)>complete_limit:
        raise ValueError('missing or truncated drilldown controls')
    counts={key:_integer(rows[0][key]) for key in COUNT_CONTROLS}
    amounts={key:_parse_decimal(rows[0][key]) for key in ('total_quantity','selected_quantity')}
    latest=_time(rows[0]['latest_shipment_at'])
    if any(counts[key] for key in QUALITY):
        raise ValueError('sales drilldown source quality failed')
    if (counts['group_count']!=len(rows) or counts['detail_count']!=len(rows)-1
        or counts['selected_sku_count']!=(1 if expected_sku else min(plan.top_n,counts['total_sku_count']))
        or not 0<=amounts['selected_quantity']<=amounts['total_quantity']
        or counts['selected_outbound_count']>counts['total_outbound_count']):
        raise ValueError('invalid ranking controls')
    if (counts['total_outbound_count']>0)!=(latest is not None):
        raise ValueError('invalid event time coverage')
    if latest is not None and not plan.start<=latest.date()<plan.end:
        raise ValueError('source event outside requested interval')
    ranking,details,seen,documents,quantities,sku_documents={},{},set(),set(),{},{}
    header_attributes={}
    control_rows=0
    for row in rows:
        if (any(_integer(row[key])!=value for key,value in counts.items())
            or any(_parse_decimal(row[key])!=value for key,value in amounts.items())
            or _time(row['latest_shipment_at'])!=latest):
            raise ValueError('inconsistent ranking controls')
        if row['row_kind']=='control':
            control_rows+=1
            if any(row[key] is not None for key in DETAIL_FIELDS):
                raise ValueError('invalid drilldown control row')
            continue
        if row['row_kind']!='detail':
            raise ValueError('invalid drilldown row kind')
        sku,owner,warehouse,outbound=(_text(row[key]) for key in ('sku_code','cargo_owner','warehouse_code','outbound_code'))
        key=(owner,outbound,sku)
        if key in seen:
            raise ValueError('duplicate drilldown business key')
        seen.add(key)
        scope=plan.scope
        if (scope.owner!='ALL' and owner!=scope.owner) or (scope.warehouse_codes and warehouse not in scope.warehouse_codes) or (scope.sku_codes and sku not in scope.sku_codes):
            raise ValueError('drilldown scope changed')
        stamp=_time(row['ship_at'])
        quantity=_parse_decimal(row['quantity'])
        if stamp is None or not plan.start<=stamp.date()<plan.end or stamp>latest or quantity<0:
            raise ValueError('invalid drilldown measure or event')
        rank=RankedSku(_integer(row['sku_rank']),sku,_text(row['sku_name']),_parse_decimal(row['sku_quantity']),_integer(row['sku_outbound_count']))
        if rank.rank<1 or rank.rank>plan.top_n or rank.quantity<0 or rank.outbound_count<1:
            raise ValueError('invalid ranked SKU')
        if sku in ranking and ranking[sku]!=rank:
            raise ValueError('ranked SKU changed between detail rows')
        ranking[sku]=rank
        details[key]=SalesDrilldownRow(sku,owner,warehouse,_text(row['warehouse_name']),_text(row['customer_code'],optional=True),outbound,stamp,quantity)
        attributes=(warehouse,details[key].warehouse_name,details[key].customer_code,stamp)
        if (owner,outbound) in header_attributes and header_attributes[owner,outbound]!=attributes:
            raise ValueError('shipment header changed between SKU details')
        header_attributes[owner,outbound]=attributes
        documents.add((owner,outbound))
        quantities[sku]=quantities.get(sku,Decimal(0))+quantity
        sku_documents.setdefault(sku,set()).add((owner,outbound))
    ordered=tuple(sorted(ranking.values(),key=lambda r:(-r.quantity,r.sku_code)))
    if control_rows!=1 or len(ordered)!=counts['selected_sku_count'] or len(details)!=counts['detail_count']:
        raise ValueError('incomplete drilldown snapshot')
    if any((expected_sku is None and r.rank!=i) or r.quantity!=quantities[r.sku_code] or r.outbound_count!=len(sku_documents[r.sku_code]) for i,r in enumerate(ordered,1)):
        raise ValueError('ranking and drilldown do not reconcile')
    if expected_sku is not None and ordered!=(expected_sku,):
        raise ValueError('original ranked SKU changed')
    if sum(quantities.values(),Decimal(0))!=amounts['selected_quantity'] or len(documents)!=counts['selected_outbound_count']:
        raise ValueError('selected totals do not reconcile')
    if expected_sku is None and counts['total_sku_count']<=plan.top_n and (amounts['selected_quantity']!=amounts['total_quantity'] or len(documents)!=counts['total_outbound_count']):
        raise ValueError('complete ranking does not match all-sales controls')
    if expected_sku is None and counts['total_sku_count']<=plan.top_n and max((r.ship_at for r in details.values()),default=None)!=latest:
        raise ValueError('complete ranking event time does not reconcile')
    if not ordered and any(counts[key] for key in ('total_sku_count','total_outbound_count')):
        raise ValueError('empty ranking has nonempty controls')
    return SalesDrilldownSnapshot(plan,ordered,tuple(details[k] for k in sorted(details)),amounts['total_quantity'],counts['total_outbound_count'],amounts['selected_quantity'],len(documents),latest)


def retrieve_sales_drilldown(plan,query,*,query_many=None,**budgets):
    try:
        rows=retrieve_sales_groups(compile_sales_drilldown(plan),query,query_many=query_many,**budgets)
        return reconcile_sales_drilldown(rows,plan,complete_limit=budgets.get('max_rows',100000))
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError('排名与回钻明细未通过完整对账，暂未展示',reason='sales_drilldown_invalid') from exc


@dataclass(frozen=True, slots=True)
class SalesRankingSnapshot:
    plan: SalesDrilldownPlan
    ranking: tuple[RankedSku,...]
    total_quantity: Decimal
    total_outbound_count: int
    selected_quantity: Decimal
    selected_outbound_count: int
    detail_count: int
    latest_shipment_at: datetime | None


def compile_sales_ranking(plan):
    tree=sqlglot.parse_one(compile_sales_drilldown(plan),read='doris')
    report=next(cte for cte in tree.args['with_'].expressions if cte.alias_or_name=='report')
    null_fields=','.join('NULL AS '+key for key in DETAIL_FIELDS[5:])
    report.set('this',sqlglot.parse_one(f"""SELECT 'rank' AS row_kind,r.sku_rank,r.sku_code,
 COALESCE(m.sku_name,r.sku_code) AS sku_name,r.sku_quantity,r.sku_outbound_count,{null_fields}
 FROM selected_skus r LEFT JOIN sku_names m ON m.sku_code=r.sku_code
 UNION ALL SELECT 'control',{','.join('NULL' for _ in DETAIL_FIELDS)}""",read='doris'))
    count=next(projection for projection in tree.selects if projection.alias_or_name=='group_count')
    count.replace(sqlglot.parse_one('1+(SELECT COUNT(*) FROM selected_skus) AS group_count',read='doris'))
    return tree.sql(dialect='doris')


def reconcile_sales_ranking(rows,plan):
    if not rows or len(rows)>plan.top_n+1:
        raise ValueError('missing ranking controls')
    counts={key:_integer(rows[0][key]) for key in COUNT_CONTROLS}
    amounts={key:_parse_decimal(rows[0][key]) for key in ('total_quantity','selected_quantity')}
    latest=_time(rows[0]['latest_shipment_at'])
    if any(counts[key] for key in QUALITY):
        raise ValueError('ranking source quality failed')
    if (counts['group_count']!=len(rows) or len(rows)!=counts['selected_sku_count']+1
        or counts['selected_sku_count']!=min(plan.top_n,counts['total_sku_count'])
        or not 0<=amounts['selected_quantity']<=amounts['total_quantity']
        or not counts['selected_outbound_count']<=counts['total_outbound_count']
        or counts['detail_count']<counts['selected_outbound_count']):
        raise ValueError('invalid ranking controls')
    if (counts['total_outbound_count']>0)!=(latest is not None) or (latest is not None and not plan.start<=latest.date()<plan.end):
        raise ValueError('invalid ranking event time')
    ranking,seen=[],set()
    controls=0
    for row in rows:
        if (any(_integer(row[key])!=value for key,value in counts.items())
            or any(_parse_decimal(row[key])!=value for key,value in amounts.items())
            or _time(row['latest_shipment_at'])!=latest):
            raise ValueError('ranking controls changed')
        if row['row_kind']=='control':
            controls+=1
            if any(row[key] is not None for key in DETAIL_FIELDS):
                raise ValueError('invalid ranking control row')
            continue
        if row['row_kind']!='rank' or any(row[key] is not None for key in DETAIL_FIELDS[5:]):
            raise ValueError('invalid ranking row')
        rank=RankedSku(_integer(row['sku_rank']),_text(row['sku_code']),_text(row['sku_name']),_parse_decimal(row['sku_quantity']),_integer(row['sku_outbound_count']))
        if rank.sku_code in seen or rank.quantity<0 or not 1<=rank.outbound_count<=counts['selected_outbound_count']:
            raise ValueError('invalid ranked measures')
        if plan.scope.sku_codes and rank.sku_code not in plan.scope.sku_codes:
            raise ValueError('ranked SKU outside scope')
        ranking.append(rank)
        seen.add(rank.sku_code)
    ordered=tuple(sorted(ranking,key=lambda r:(-r.quantity,r.sku_code)))
    if controls!=1 or any(row.rank!=i for i,row in enumerate(ordered,1)):
        raise ValueError('incomplete or unstable ranking')
    if (sum((r.quantity for r in ordered),Decimal(0))!=amounts['selected_quantity']
        or sum(r.outbound_count for r in ordered)!=counts['detail_count']):
        raise ValueError('ranked quantities or document-SKU counts do not reconcile')
    if counts['total_sku_count']<=plan.top_n and (amounts['selected_quantity']!=amounts['total_quantity'] or counts['selected_outbound_count']!=counts['total_outbound_count']):
        raise ValueError('ranking does not match all-sales controls')
    if not ordered and any(counts[key] for key in ('total_sku_count','total_outbound_count','detail_count','selected_outbound_count')):
        raise ValueError('empty ranking has nonempty controls')
    return SalesRankingSnapshot(plan,ordered,amounts['total_quantity'],counts['total_outbound_count'],amounts['selected_quantity'],counts['selected_outbound_count'],counts['detail_count'],latest)


def retrieve_sales_ranking(plan,query,*,query_many=None,**budgets):
    try:
        return reconcile_sales_ranking(retrieve_sales_groups(compile_sales_ranking(plan),query,query_many=query_many,**budgets),plan)
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError('销售排名未通过完整对账，暂未展示',reason='sales_ranking_invalid') from exc


def retrieve_ranked_sku(ranking,sku_code,query,*,query_many=None,**budgets):
    expected=next((row for row in ranking.ranking if row.sku_code==sku_code),None)
    if expected is None:
        raise KeyError('SKU is not in this ranking snapshot')
    max_rows=budgets.get('max_rows',100000)
    if type(max_rows) is not int or not 1<=max_rows<=100000:
        raise ValueError('invalid drilldown capacity')
    if expected.outbound_count+1>max_rows:
        raise SemanticRuntimeError('该SKU的完整明细超过处理容量，未返回部分结果',reason='result_truncated')
    tree=sqlglot.parse_one(compile_sales_drilldown(ranking.plan),read='doris')
    selected=next(cte for cte in tree.args['with_'].expressions if cte.alias_or_name=='selected_skus')
    # Apply the clicked key after ranking; do not rerank or rebuild user scope.
    selected.this.where(exp.column('sku_code').eq(exp.Literal.string(sku_code)),append=True,copy=False)
    try:
        rows=retrieve_sales_groups(tree.sql(dialect='doris'),query,query_many=query_many,**budgets)
        detail=reconcile_sales_drilldown(rows,ranking.plan,complete_limit=budgets.get('max_rows',100000),expected_sku=expected)
        if (detail.total_quantity,detail.total_outbound_count,detail.latest_shipment_at)!=(ranking.total_quantity,ranking.total_outbound_count,ranking.latest_shipment_at):
            raise ValueError('original all-sales controls changed')
        return detail
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError('数据已变化或明细与原排名不一致，请重新查询排名',reason='sales_drilldown_changed') from exc
