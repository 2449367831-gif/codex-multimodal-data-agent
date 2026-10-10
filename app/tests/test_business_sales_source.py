"""Synthetic SQL regressions for replacement, time boundaries and join guards."""
from datetime import date
from pathlib import Path
import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from app.sales_source import bind_business_sales
from app.query_scope import QueryScope
from app.agent_core.execution import QueryExecutionSession
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.agent_core.handlers.business_sales import BusinessSalesHandler

ROOT = Path(__file__).resolve().parents[2]
START, END = date(2026, 10, 9), date(2026, 10, 10)


@pytest.fixture
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    c.executescript("""
    CREATE TABLE ywzt_ods.scm_sales_shipment_header(company_code,facility_code,ship_at,outbound_code,ref_order_code,store_code,status INTEGER,ref_order_type);
    CREATE TABLE ywzt_ods.scm_sales_shipment_detail(outbound_code,sku_code,ship_qty);
    CREATE TABLE ywzt_ods.scm_sales_order_header(company_code,facility_code,created,sales_order_code,store_code,_fdl_marked_deleted);
    CREATE TABLE ywzt_ods.scm_sales_order_detail(sales_order_code,sku_code,request_qty,status,_fdl_marked_deleted);
    CREATE TABLE ywzt_ods.master_store(code,type,platform_code,group1,price_group);
    CREATE TABLE ywzt_ods.master_sku(sku_code,sku_name,sku_group);
    CREATE TABLE ywzt_ods.master_wms(wms_code,wms_desc);
    INSERT INTO ywzt_ods.master_store VALUES ('online','Z004','DY','',''),('community','Z001',NULL,'03',''),('distribution','Z001',NULL,'04','122');
    INSERT INTO ywzt_ods.master_sku VALUES ('A','测试甲','50'),('B','测试乙','50');
    INSERT INTO ywzt_ods.master_wms VALUES ('W1','测试仓一'),('W2','测试仓二');
    INSERT INTO ywzt_ods.scm_sales_order_header VALUES
      ('ONE','W1','2026-10-10 00:00:00','O1','online','false'),
      ('ONE','W1','2026-10-10 00:00:00','C1','community','false'),
      ('ONE','W1','2026-10-11 00:00:00','C2','community','false'),
      ('ONE','W1','2026-10-09 23:59:59','C3','community','false'),
      ('TWO','W2','2026-10-10 23:59:59','C4','community','false'),
      ('ONE','W1','2026-10-10 12:00:00','C5','community','true');
    INSERT INTO ywzt_ods.scm_sales_order_detail VALUES
      ('O1','A',1000,0,'false'),('C1','A',12,0,'false'),('C1','B',3,0,NULL),
      ('C1','A',999,1,'false'),('C1','A',888,0,'true'),
      ('C2','A',100,0,'false'),('C3','A',200,0,'false'),('C4','A',7,0,'false'),('C5','A',800,0,'false');
    INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES
      ('ONE','W1','2026-10-09 00:00:00','S1','O1','online',700,'JYCK'),
      ('ONE','W1','2026-10-09 12:00:00','S2','C1','community',900,'SO07'),
      ('ONE','W1','2026-10-10 00:00:00','S3','O1','online',700,'JYCK'),
      ('ONE','W1','2026-10-09 12:00:00','S4','O1','distribution',701,'HHCK'),
      ('ONE','W1','2026-10-09 12:00:00','S5','O1',NULL,901,'QTCK');
    INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES
      ('S1','A',10),('S1','B',5),('S2','A',99),('S3','A',300),('S4','A',4),('S5','A',2);
    """)
    yield c
    c.close()


def execute(db, sql):
    tree = sqlglot.parse_one(sql, read="doris")
    for node in list(tree.find_all(exp.DateSub)):
        node.replace(exp.Anonymous(this="DATETIME", expressions=[node.this.copy(), exp.Literal.string("-1 day")]))
    return [dict(r) for r in db.execute(tree.sql(dialect="sqlite"))]


def test_boundary_replacement_and_source_status(db):
    sql = bind_business_sales("SELECT source_kind, SUM(quantity) AS quantity FROM sales_current GROUP BY source_kind", START, END)
    rows = execute(db, sql)
    assert {r['source_kind']:r['quantity'] for r in rows} == {'shipment':21, 'community_order':22}
    assert all(r['__sales_join_conflicts'] == 0 for r in rows)
    # The current end date shifts to tomorrow on the raw order-created field.
    assert "h.created < '2026-10-11 00:00:00'" in sql


def test_compiler_matches_original_sql(db):
    original = sqlglot.parse_one((ROOT / 'sql_templates/sales.business_quantity.sql').read_text(encoding='utf8'), read='doris')
    values = dict(start_inclusive='2026-10-09 00:00:00', end_exclusive='2026-10-10 00:00:00', community_start='2026-10-10 00:00:00', community_end='2026-10-11 00:00:00')
    for p in list(original.find_all(exp.Placeholder)):
        p.replace(exp.Literal.string(values[p.name]))
    expected = execute(db, original.sql(dialect='doris'))
    actual = execute(db, bind_business_sales("SELECT wms_code,sku_code,SUM(quantity) AS quantity FROM sales_current GROUP BY wms_code,sku_code", START,END))
    assert {(r['仓库编码'],r['商品编码']):r['总发货量'] for r in expected} == {(r['wms_code'],r['sku_code']):r['quantity'] for r in actual}
    assert all(r['总发货量'] == r['线上汇总'] + r['线下汇总'] + r['其它'] for r in expected)


def test_scope_independent_for_both_sources(db):
    scoped = bind_business_sales("SELECT SUM(quantity) AS n FROM sales_current",START,END,
        scope=QueryScope(owner='ONE',warehouse_codes=('W1',),sku_codes=('A',)))
    assert execute(db,scoped)[0]['n'] == 28
    unscoped = bind_business_sales("SELECT SUM(quantity) AS n FROM sales_current",START,END)
    assert execute(db,unscoped)[0]['n'] == 43


def test_duplicate_join_blocks_instead_of_silent_max(db):
    db.execute("INSERT INTO ywzt_ods.scm_sales_order_header SELECT * FROM ywzt_ods.scm_sales_order_header WHERE sales_order_code='O1'")
    rows=execute(db,bind_business_sales("SELECT SUM(quantity) AS n FROM sales_current",START,END))
    assert rows[0]['__sales_join_conflicts'] > 0
    payload={'status':'succeeded','execution':{'columns':list(rows[0]),'rows':rows,'row_count':1,'truncated':False,'is_mock':False,'executor_name':'doris'}}
    with pytest.raises(SemanticRuntimeError,match='重复'):
        QueryExecutionSession(None,QueryScope(),'HJXH')._record_response(payload,business=True)


def test_rule_counts_keep_order_and_outbound_separate(db):
    session=QueryExecutionSession(None,QueryScope(),'HJXH')
    request=SemanticRequest('business_sales_top_sku',START,END,show_top_sku=True,top_n=20)
    rows=execute(db,BusinessSalesHandler(session,timezone_name='Asia/Shanghai')._sql(request))
    assert rows[0]['sku_code']=='A'
    assert rows[0]['quantity']==35
    assert rows[0]['outbound_documents']==3
    assert rows[0]['community_orders']==2
    assert rows[0]['all_quantity']==43


def test_warehouse_rule_does_not_scope_to_legacy_owner(db):
    session=QueryExecutionSession(None,QueryScope(),'HJXH')
    request=SemanticRequest('business_sales_by_warehouse',START,END,show_warehouses=True)
    rows=execute(db,BusinessSalesHandler(session,timezone_name='Asia/Shanghai')._sql(request))
    assert {r['wms_code'] for r in rows} == {'W1','W2'}


def test_public_table_retains_both_components_and_exports(db):
    from app.chat_api import _public_business_table
    class Checked:
        legacy_owner = 'HJXH'
        scope = QueryScope()
        def _query_checked(self, sql, **kwargs):
            return execute(db, sql)
    handler = BusinessSalesHandler(Checked(), timezone_name='Asia/Shanghai')
    for top in (False, True):
        answer = handler.run(SemanticRequest('business_sales_top_sku' if top else 'business_sales_by_warehouse',
            START, END, show_top_sku=top, top_n=20 if top else None, show_warehouses=not top))
        public = _public_business_table(answer.business_table, live=True)
        assert public is not None
        assert len(public['rows']) == 2
        assert {'shipment_quantity','community_quantity','outbound_documents','community_orders'} <= set(public['rows'][0])
        assert '社区' in public['caption']
        assert '__sales_join_conflicts' not in str(public)


def test_requested_scope_filters_do_not_touch_reference_order_join(db):
    from app.query_scope import apply_scope
    sql=bind_business_sales('SELECT SUM(quantity) AS n FROM sales_current',START,END,
                           scope=QueryScope(owner='ONE', warehouse_codes=('W1',),sku_codes=('A',)))
    checked=apply_scope(sql,QueryScope(owner='ONE',warehouse_codes=('W1',),sku_codes=('A',)),legacy_owner='HJXH')
    assert execute(db,checked)[0]['n']==28


def test_model_context_contains_new_basis_and_complete_reference_template():
    import json
    from app.codex_agent import codex_knowledge_context
    context=json.loads(codex_knowledge_context(ROOT,question='查询昨天销售数量，按渠道汇总',compact=True))
    assert 'sales.business_quantity' in {m['id'] for m in context['metrics']}
    assert {'sql_templates/sales.business_quantity.facts.sql','sql_templates/sales.business_quantity.sql'} <= {t['path'] for t in context['templates']}
    assert context['rules']['rules/sales-default.yaml']['default_metric']=='sales.business_quantity@1.0.0'
    assert context['retrieval']['selection_authority']=='model'
