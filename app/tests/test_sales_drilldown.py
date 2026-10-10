"""Requirements-first synthetic tests for one reconciled ranking/detail snapshot."""

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest
import sqlglot

from app.agent_core.sales_drilldown import (
    SalesDrilldownPlan, compile_sales_drilldown, reconcile_sales_drilldown,
    retrieve_sales_drilldown,
)
from app.agent_core.semantic_types import SemanticRuntimeError
from app.query_scope import QueryScope
from app.tests.test_sales_aggregation import facts, candidate_scope_rules


@pytest.fixture
def source(facts):
    facts.execute('ALTER TABLE ywzt_ods.scm_sales_shipment_header ADD COLUMN store_code TEXT')
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code='C1'")
    return facts


def plan(**kwargs):
    return SalesDrilldownPlan(date(2026,9,9),date(2026,9,10),**kwargs)


def query(db,sql):
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


def checked(db,p=None):
    p=p or plan()
    return reconcile_sales_drilldown(query(db,compile_sales_drilldown(p)),p)


def test_same_fact_snapshot_reconciles_ranking_and_every_drilldown(source):
    result=checked(source)
    assert result.total_quantity==Decimal('13.75')
    assert result.total_outbound_count==4
    assert [(r.rank,r.sku_code,r.quantity,r.outbound_count) for r in result.ranking]==[(1,'A',Decimal('13.75'),4)]
    details=result.drilldown('A')
    assert len(details)==4
    assert sum(r.quantity for r in details)==result.ranking[0].quantity
    assert {(r.cargo_owner,r.outbound_code) for r in details}=={('HJXH','O1'),('HJXH','O2'),('HJXH','O3'),('B','O1')}
    assert {r.customer_code for r in details}=={'C1'}
    assert all(r.ship_at.date()==date(2026,9,9) for r in details)
    assert sum(r.quantity==0 for r in details)==2


def test_top_n_is_applied_after_aggregation_with_stable_sku_tie_break(source):
    source.execute("UPDATE ywzt_ods.master_sku SET sku_group='50'")
    source.execute("INSERT INTO ywzt_ods.master_sku VALUES ('C','Product C','50')")
    source.execute("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES ('HJXH','O1','C',4.25,0,'false')")
    result=checked(source,plan(top_n=2))
    assert [r.sku_code for r in result.ranking]==['A','B']
    assert result.total_quantity==Decimal('22.25')
    assert result.selected_quantity==Decimal('18')
    assert result.selected_outbound_count==4
    assert sum(r.outbound_count for r in result.ranking)==5
    with pytest.raises(KeyError):result.drilldown('C')


def test_scope_is_rebuilt_without_inheriting_owner_or_warehouse(source):
    first=checked(source,plan(scope=QueryScope(owner='HJXH',warehouse_codes=('W1',),finished_goods=True)))
    second=checked(source)
    assert first.total_quantity==Decimal('3.75')
    assert first.total_outbound_count==2
    assert second.total_quantity==Decimal('13.75')
    assert second.plan.scope.owner=='ALL'


def test_customer_conflicting_header_blocks_instead_of_arbitrary_min(source):
    source.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,outbound_code,facility_code,ship_at,status,ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted,'C2' FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O1' AND company_code='HJXH' LIMIT 1")
    with pytest.raises(ValueError,match='quality'):checked(source)


@pytest.mark.parametrize('kind',['name','category','warehouse','null_quantity','negative_quantity'])
def test_source_conflicts_and_bad_quantities_cannot_be_hidden(source,kind):
    if kind=='name':source.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A','Other name','50')")
    if kind=='category':source.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A','测试成品','60')")
    if kind=='warehouse':source.execute("INSERT INTO ywzt_ods.master_wms VALUES ('HJXH','W1','Other warehouse','0')")
    if kind=='null_quantity':source.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=NULL WHERE company_code='B'")
    if kind=='negative_quantity':source.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=-1 WHERE company_code='B'")
    with pytest.raises(ValueError,match='quality'):checked(source)


def test_duplicate_master_names_do_not_multiply_and_missing_customer_is_preserved(source):
    source.execute("INSERT INTO ywzt_ods.master_sku SELECT * FROM ywzt_ods.master_sku WHERE sku_code='A'")
    source.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code=NULL WHERE outbound_code='O2'")
    result=checked(source)
    assert result.total_quantity==Decimal('13.75')
    assert sum(r.customer_code is None for r in result.drilldown('A'))==1


def test_snapshot_is_immutable_and_never_requeries_on_drilldown(source):
    result=checked(source)
    source.execute('DELETE FROM ywzt_ods.scm_sales_shipment_detail')
    assert sum(r.quantity for r in result.drilldown('A'))==Decimal('13.75')
    with pytest.raises(FrozenInstanceError):result.ranking[0].quantity=Decimal(0)
    with pytest.raises(FrozenInstanceError):result.drilldown('A')[0].customer_code='C2'
    assert isinstance(result.ranking,tuple) and isinstance(result.details,tuple)


def test_empty_success_requires_control_row(source):
    source.execute('DELETE FROM ywzt_ods.scm_sales_shipment_detail')
    result=checked(source)
    assert result.ranking==result.details==() and result.total_quantity==0
    with pytest.raises(ValueError):reconcile_sales_drilldown([],plan())


@pytest.mark.parametrize('field,value',[
    ('quantity','99'),('sku_quantity','99'),('sku_outbound_count',99),
    ('sku_rank',2),('selected_quantity','99'),('selected_outbound_count',99),
    ('total_quantity','0'),('total_outbound_count',0),('group_count',99),
    ('ship_at','2026-09-10 00:00:00'),('warehouse_code',None),
    ('row_kind','unknown'),('sku_code','UNKNOWN'),
])
def test_changed_result_controls_are_rejected_before_snapshot(source,field,value):
    p=plan()
    rows=query(source,compile_sales_drilldown(p))
    detail=next(r for r in rows if r['row_kind']=='detail')
    detail[field]=value
    with pytest.raises(ValueError):reconcile_sales_drilldown(rows,p)


def test_missing_or_duplicate_detail_cannot_pass(source):
    p=plan()
    rows=query(source,compile_sales_drilldown(p))
    with pytest.raises(ValueError):reconcile_sales_drilldown(rows[:-1],p)
    rows[-1]=dict(rows[-2])
    with pytest.raises(ValueError):reconcile_sales_drilldown(rows,p)


def test_bounded_pages_are_complete_and_source_change_returns_no_snapshot(source):
    calls=[]
    def read(sql,*,purpose):
        calls.append(sql)
        return query(source,sql)
    result=retrieve_sales_drilldown(plan(),read,page_size=2)
    assert result==checked(source) and len(calls)==6
    calls.clear()
    def changing(sql,*,purpose):
        if len(calls)==3:
            source.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=11 WHERE company_code='B'")
        return read(sql,purpose=purpose)
    with pytest.raises(SemanticRuntimeError):retrieve_sales_drilldown(plan(),changing,page_size=2)


def test_capacity_never_silently_returns_only_some_top20_details(source):
    def read(sql,*,purpose):return query(source,sql)
    with pytest.raises(SemanticRuntimeError) as error:
        retrieve_sales_drilldown(plan(),read,max_rows=3,page_size=2)
    assert error.value.reason=='result_truncated'


@pytest.mark.parametrize('kwargs',[
    {'top_n':0},{'top_n':101},{'top_n':True},
    {'scope':QueryScope(channel='online',finished_goods=True)},
    {'scope':QueryScope(unit='box',finished_goods=True)},
    {'scope':QueryScope(finished_goods=False)},
    {'scope':QueryScope(finished_goods=True,finished_goods_exceptions=('OTHER',))},
])
def test_unsupported_shape_fails_before_database(kwargs):
    with pytest.raises(ValueError):plan(**kwargs)


def test_sql_uses_one_readonly_statement_qualified_sources_and_no_allocation():
    from sqlglot import exp
    tree=sqlglot.parse_one(compile_sales_drilldown(plan()),read='doris')
    physical=[t for t in tree.find_all(exp.Table) if t.db]
    assert {t.db for t in physical}=={'ywzt_ods'}
    assert not any('allocate' in t.name for t in physical)
    assert {t.name for t in physical}>={'scm_sales_shipment_header','scm_sales_shipment_detail','scm_sales_order_header','master_sku','master_wms'}


def test_independent_reference_matches_all_fields_before_live(source):
    from importlib.util import module_from_spec,spec_from_file_location
    from pathlib import Path
    spec=spec_from_file_location('sales_drilldown_reference',Path(__file__).resolve().parents[2]/'tests/acceptance/check_sales_drilldown_live.py')
    reference=module_from_spec(spec)
    spec.loader.exec_module(reference)
    actual=checked(source)
    rows=query(source,reference.reference_sql(plan()))
    assert all(reference.compare(actual,rows).values())
    rows[0]['customer_code']='WRONG'
    assert not reference.compare(actual,rows)['every_detail_field']


def test_control_row_does_not_coerce_business_decimal_precision():
    from sqlglot import exp
    tree=sqlglot.parse_one(compile_sales_drilldown(plan()),read='doris')
    assert not any(cast.to.is_type('DECIMAL') for cast in tree.find_all(exp.Cast))


def test_same_document_cannot_change_customer_between_skus(source):
    source.execute("UPDATE ywzt_ods.master_sku SET sku_group='50'")
    rows=query(source,compile_sales_drilldown(plan()))
    next(r for r in rows if r['row_kind']=='detail' and r['sku_code']=='B')['customer_code']='C2'
    with pytest.raises(ValueError,match='header changed'):reconcile_sales_drilldown(rows,plan())


def test_full_ranking_freshness_reconciles_not_just_in_window(source):
    rows=query(source,compile_sales_drilldown(plan()))
    for row in rows:row['latest_shipment_at']='2026-09-09 23:59:59.999999'
    with pytest.raises(ValueError,match='event time'):reconcile_sales_drilldown(rows,plan())


def test_ranking_budget_does_not_require_prefetching_all_document_details(source):
    from app.agent_core.sales_drilldown import retrieve_sales_ranking, retrieve_ranked_sku
    calls=[]
    def read(sql,*,purpose):
        calls.append(sql)
        return query(source,sql)
    ranking=retrieve_sales_ranking(plan(),read,max_rows=2,page_size=2)
    assert len(ranking.ranking)==1 and ranking.detail_count==4 and len(calls)==2
    detail=retrieve_ranked_sku(ranking,'A',read,page_size=2)
    assert detail.ranking==ranking.ranking and len(detail.drilldown('A'))==4


def test_clicked_sku_keeps_original_rank_not_reranked_one(source):
    from app.agent_core.sales_drilldown import retrieve_sales_ranking, retrieve_ranked_sku
    source.execute("UPDATE ywzt_ods.master_sku SET sku_group='50'")
    def read(sql,*,purpose):return query(source,sql)
    ranking=retrieve_sales_ranking(plan(),read)
    detail=retrieve_ranked_sku(ranking,'B',read)
    assert detail.ranking==(ranking.ranking[1],) and detail.ranking[0].rank==2
    assert len(detail.details)==1 and detail.selected_quantity==Decimal('4.25')
    assert detail.plan==ranking.plan


def test_clicking_unknown_sku_never_queries_or_broadens_scope(source):
    from app.agent_core.sales_drilldown import retrieve_sales_ranking, retrieve_ranked_sku
    ranking=retrieve_sales_ranking(plan(),lambda sql,**kw:query(source,sql))
    def forbidden(*args,**kwargs):pytest.fail('unranked SKU reached Doris')
    with pytest.raises(KeyError):retrieve_ranked_sku(ranking,'B',forbidden)


@pytest.mark.parametrize('change',['quantity','rank','customer','name','deleted'])
def test_changed_ranking_or_details_never_silently_replace_original(source,change):
    from app.agent_core.sales_drilldown import retrieve_sales_ranking, retrieve_ranked_sku
    source.execute("UPDATE ywzt_ods.master_sku SET sku_group='50'")
    def read(sql,*,purpose):return query(source,sql)
    ranking=retrieve_sales_ranking(plan(),read)
    if change=='quantity':source.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=12 WHERE company_code='B'")
    if change=='rank':source.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=100 WHERE sku_code='B' AND outbound_code='O1'")
    if change=='customer':
        # A conflicting header, not an assumed old customer-master classification.
        source.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,outbound_code,facility_code,ship_at,status,ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted,'C2' FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O1' AND company_code='B'")
    if change=='name':source.execute("UPDATE ywzt_ods.master_sku SET sku_name='Changed' WHERE sku_code='A'")
    if change=='deleted':source.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET delete_flag=1 WHERE company_code='B'")
    with pytest.raises(SemanticRuntimeError):retrieve_ranked_sku(ranking,'A',read)
    assert ranking.ranking[0].quantity==Decimal('13.75')


def test_independent_ranking_and_selected_detail_preserve_all_scope(source):
    from importlib.util import module_from_spec,spec_from_file_location
    from pathlib import Path
    from app.agent_core.sales_drilldown import retrieve_sales_ranking,retrieve_ranked_sku
    spec=spec_from_file_location('staged_drilldown_reference',Path(__file__).resolve().parents[2]/'tests/acceptance/check_sales_drilldown_live.py')
    reference=module_from_spec(spec)
    spec.loader.exec_module(reference)
    source.execute("UPDATE ywzt_ods.master_sku SET sku_group='50'")
    def read(sql,*,purpose):return query(source,sql)
    ranking=retrieve_sales_ranking(plan(),read)
    assert all(reference.compare_ranking(ranking,query(source,reference.reference_ranking_sql(plan()))).values())
    for sku in ranking.ranking:
        detail=retrieve_ranked_sku(ranking,sku.sku_code,read)
        assert all(reference.compare(detail,query(source,reference.reference_sql(plan(),sku.sku_code))).values())


def test_known_oversized_drilldown_fails_before_another_query(source):
    from app.agent_core.sales_drilldown import retrieve_sales_ranking,retrieve_ranked_sku
    ranking=retrieve_sales_ranking(plan(),lambda sql,**kw:query(source,sql))
    def forbidden(*args,**kwargs):pytest.fail('known oversized detail reached Doris')
    with pytest.raises(SemanticRuntimeError) as error:
        retrieve_ranked_sku(ranking,'A',forbidden,max_rows=4)
    assert error.value.reason=='result_truncated'


@pytest.mark.parametrize('field,value',[
    ('sku_rank',2),('sku_quantity','100'),('sku_outbound_count',5),
    ('group_count',3),('detail_count',5),('selected_sku_count',0),
    ('total_quantity',0),('header_conflict_count',1),('sku_name',None),
    ('customer_code','UNEXPECTED'),('latest_shipment_at',None),
])
def test_ranking_only_controls_reject_corruption(source,field,value):
    from app.agent_core.sales_drilldown import compile_sales_ranking,reconcile_sales_ranking
    rows=query(source,compile_sales_ranking(plan()))
    next(row for row in rows if row['row_kind']=='rank')[field]=value
    with pytest.raises(ValueError):reconcile_sales_ranking(rows,plan())


def test_ranking_only_empty_result_still_has_verified_controls(source):
    from app.agent_core.sales_drilldown import compile_sales_ranking,reconcile_sales_ranking
    source.execute('DELETE FROM ywzt_ods.scm_sales_shipment_detail')
    result=reconcile_sales_ranking(query(source,compile_sales_ranking(plan())),plan())
    assert result.ranking==() and result.total_quantity==0 and result.detail_count==0
