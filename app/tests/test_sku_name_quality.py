from datetime import date
from decimal import Decimal

import pytest
import sqlglot

from app.tests.test_sales_aggregation import facts
from app.query_scope import QueryScope
from app.agent_core.sku_name_quality import compile_sku_name_quality, reconcile_sku_names


@pytest.fixture
def named_facts(facts):
    facts.execute("ALTER TABLE ywzt_ods.scm_sales_shipment_detail ADD COLUMN sku_name TEXT")
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET sku_name='Shipment A' WHERE sku_code='A'")
    facts.execute("UPDATE ywzt_ods.master_sku SET sku_name='Master A' WHERE sku_code='A'")
    facts.execute("CREATE TABLE ywzt_ods.scm_sales_order_detail (company_code TEXT,sales_order_code TEXT,sku_code TEXT,sku_name TEXT,_fdl_marked_deleted TEXT)")
    facts.executemany("INSERT INTO ywzt_ods.scm_sales_order_detail VALUES (?,?,?,?,?)", [
        ('HJXH','S1','A','Order A','false'),('HJXH','S1','A','Order A','false'),
        ('HJXH','S2','A',None,'false'),('HJXH','S3','A','Order A','false'),
        ('B','S1','A','Other owner A','false'),('HJXH','S1','B','Order B','false'),
        ('HJXH','S1','A','Deleted A','true'),('HJXH','S4','A','Outside interval','false'),
    ])
    return facts


def query(db, scope=QueryScope()):
    sql = compile_sku_name_quality(start=date(2026,9,9),end=date(2026,9,10),scope=scope)
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


def test_names_never_multiply_or_split_sales_and_keep_owner_separation(named_facts):
    rows=query(named_facts)
    assert len(rows)==3
    assert sum(Decimal(str(row['quantity'])) for row in rows)==18
    a=next(row for row in rows if row['cargo_owner']=='HJXH' and row['sku_code']=='A')
    assert a['quantity']==3.75 and a['outbound_count']==3
    assert a['master_name']=='Master A' and a['order_name']=='Order A'
    assert a['shipment_name']=='Shipment A' and a['all_name_variants']==3
    assert a['order_rows']==4 and a['order_missing']==1
    assert a['order_variants']==1
    b=next(row for row in rows if row['cargo_owner']=='B')
    assert b['order_rows']==1 and b['order_name']=='Other owner A'
    resolved=reconcile_sku_names(rows)
    assert resolved[0]['sku_name'] and all(row['name_conflict'] for row in resolved)


def test_one_order_shipped_twice_does_not_repeat_order_observations(named_facts):
    named_facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET ref_order_code='S1' WHERE outbound_code='O2'")
    row=next(row for row in query(named_facts) if row['cargo_owner']=='HJXH' and row['sku_code']=='A')
    assert row['outbound_count']==3 and row['order_rows']==3
    assert row['order_missing']==0


def test_duplicate_master_names_are_not_conflicts_but_different_names_are(named_facts):
    named_facts.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A','Master A','50')")
    rows=query(named_facts, QueryScope(sku_codes=('A',),owner='HJXH'))
    assert len(rows)==1 and rows[0]['master_rows']==2 and rows[0]['master_variants']==1
    assert rows[0]['quantity']==3.75
    named_facts.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A','Another master name','50')")
    resolved=reconcile_sku_names(query(named_facts, QueryScope(sku_codes=('A',),owner='HJXH')))
    assert resolved[0]['sku_name']=='A' and resolved[0]['name_source']=='sku_code'
    assert resolved[0]['master_name'] is None and resolved[0]['name_conflict']


def test_missing_sources_are_reported_without_invented_fallback_names(named_facts):
    named_facts.execute("DELETE FROM ywzt_ods.master_sku WHERE sku_code='A'")
    named_facts.execute("DELETE FROM ywzt_ods.scm_sales_order_detail")
    resolved=reconcile_sku_names(query(named_facts, QueryScope(sku_codes=('A',),owner='HJXH')))
    assert resolved[0]['sku_name']=='A'
    assert resolved[0]['master_rows']==resolved[0]['order_rows']==0
    assert resolved[0]['name_missing'] and resolved[0]['quantity']=='3.75'


def test_names_cannot_match_another_sku_code(named_facts):
    named_facts.execute("INSERT INTO ywzt_ods.master_sku VALUES ('NOT-A','A','50')")
    assert query(named_facts, QueryScope(sku_codes=('NOT-A',)))==[]


def test_fact_product_filters_do_not_hide_alternative_master_names(named_facts):
    named_facts.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A','Conflicting master','60')")
    rows=query(named_facts,QueryScope(owner='HJXH',finished_goods=True))
    assert len(rows)==1 and rows[0]['master_variants']==2
    assert rows[0]['quantity']==3.75


@pytest.mark.parametrize('key,value', [('quantity','99'),('master_variants',-1),('order_missing',999),('all_name_variants',0),('master_name',''),('header_conflict_count',1),('group_count',999)])
def test_invalid_name_or_quantity_controls_block_output(named_facts,key,value):
    rows=query(named_facts)
    rows[0][key]=value
    with pytest.raises(ValueError):
        reconcile_sku_names(rows)


def test_duplicate_groups_and_truncation_cannot_look_complete(named_facts):
    rows=query(named_facts)
    with pytest.raises(ValueError): reconcile_sku_names(rows[:-1])
    with pytest.raises(ValueError): reconcile_sku_names(rows+[rows[0]])
    assert reconcile_sku_names([])==[]


def test_names_respect_exact_scope_and_time_not_previous_question(named_facts):
    narrow=query(named_facts,QueryScope(owner='HJXH',warehouse_codes=('W1',),sku_codes=('A',)))
    assert len(narrow)==1 and narrow[0]['outbound_count']==2
    assert len(query(named_facts))==3


def test_generated_query_is_one_fully_qualified_read_only_statement():
    sql=compile_sku_name_quality(start=date(2026,9,9),end=date(2026,9,10),scope=QueryScope(sku_codes=("A' OR 1=1 --",)))
    statements=sqlglot.parse(sql,read='doris')
    assert len(statements)==1
    tree=statements[0]
    assert isinstance(tree,sqlglot.exp.Select)
    assert all(t.db=='ywzt_ods' for t in tree.find_all(sqlglot.exp.Table) if t.name.startswith(('scm_','master_')))
    assert "A' OR 1=1 --" in [value.this for value in tree.find_all(sqlglot.exp.Literal)]


def test_chat_scope_independent_reference_uses_only_the_exact_code(named_facts):
    import runpy
    from pathlib import Path
    script=runpy.run_path(str(Path(__file__).resolve().parents[2]/'tests/acceptance/check_sku_scope_chat_live.py'))
    named_facts.execute("UPDATE ywzt_ods.scm_sales_shipment_detail SET sku_code='QY-YWKC500G' WHERE sku_code='A'")
    sql=script['reference_sql'](date(2026,9,9),date(2026,9,10))
    rows=[dict(row) for row in named_facts.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]
    assert rows==[{'sku':'QY-YWKC500G','quantity':13.75,'docs':4}]
