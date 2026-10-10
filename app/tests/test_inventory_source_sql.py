"""Execute the approved inventory arithmetic against synthetic source facts."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from app.inventory_source import TEMPLATE, bind_current_inventory
from app.query_scope import QueryScope, apply_scope
from app.agent_core.execution import QueryExecutionSession
from app.agent_core.semantic_types import SemanticRuntimeError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inventory_db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.create_function('REGEXP', 2, lambda pattern, text: bool(re.search(pattern, str(text or ''))))
    db.create_function('REGEXP_LIKE', 2, lambda text, pattern: bool(re.search(pattern.replace('\\\\', '\\'), str(text or ''))))
    db.create_function('GREATEST', -1, max)
    db.create_function('NOW', 0, lambda: datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
    db.execute("ATTACH ':memory:' AS ywzt_ods")
    tables = {
        'scm_warehous_manage': 'wms_code TEXT,cargo_owner_id TEXT,sku_code TEXT,in_stock_quantity REAL,stock_lock TEXT,freeze_quantity REAL,inventory_status TEXT,stock_change_time TEXT',
        'scm_warehous_occupy': 'wms_code TEXT,cargo_owner_id TEXT,sku_code TEXT,freeze_quantity REAL,created_date TEXT,status TEXT,pre_order_code TEXT',
        'scm_po_header': 'po_code TEXT,take_delivery_wms TEXT,cargo_owner_id TEXT,order_status TEXT',
        'scm_po_detail': 'po_code TEXT,sku_code TEXT,request_quantity REAL,status TEXT',
        'scm_transfer_order_header': 'transfer_order_code TEXT,receive_wms TEXT,receiver_cargo_owner TEXT,order_status TEXT,outbound_code TEXT',
        'scm_transfer_order_detail': 'transfer_order_code TEXT,sku_code TEXT',
        'scm_sales_shipment_allocate_wms': 'outbound_code TEXT,cargo_owner_id TEXT,sku_code TEXT,allocate_qty REAL,_fdl_marked_deleted TEXT,wms_code TEXT',
        'scm_stock_history': 'wms_code TEXT,cargo_owner_id TEXT,sku_code TEXT',
        'master_wms': 'wms_code TEXT,cargo_owner_id TEXT,wms_desc TEXT,wms_type TEXT',
    }
    for table, columns in tables.items():
        db.execute(f'CREATE TABLE ywzt_ods.{table} ({columns})')
    db.execute("INSERT INTO ywzt_ods.master_wms VALUES ('RECEIVE','HJXH','Receiving warehouse','10')")
    for row in [('S1',100,'0',4,'ZP'),('S1',20,'0',6,None),('S2',5,'2',99,''),('S3',50,'nonnumeric',7,'ZP'),('S1',999,'0',0,'BAD')]:
        db.execute("INSERT INTO ywzt_ods.scm_warehous_manage VALUES ('RECEIVE','HJXH',?,?,?,?,?,'2026-10-10 01:00:00')",row)
    recent=(datetime.now(timezone.utc)-timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    old=(datetime.now(timezone.utc)-timedelta(days=61)).strftime('%Y-%m-%d %H:%M:%S')
    for sku,qty,when,status,code in [('S1',15,recent,'0','SALE'),('S1',5,recent,'0','HJXH-TH1'),('S1',999,recent,None,'SALE'),('S1',999,old,'0','SALE'),('S1',999,recent,'1','SALE'),('S2',10,recent,'0','SALE')]:
        db.execute("INSERT INTO ywzt_ods.scm_warehous_occupy VALUES ('RECEIVE','HJXH',?,?,?,?,?)",(sku,qty,when,status,code))
    db.execute("INSERT INTO ywzt_ods.scm_po_header VALUES ('PO','RECEIVE','HJXH','12')")
    db.execute("INSERT INTO ywzt_ods.scm_po_detail VALUES ('PO','S2',2,'0'),('PO','TRANSIT_ONLY',11,'0'),('PO','S2',999,'1')")
    db.execute("INSERT INTO ywzt_ods.scm_transfer_order_header VALUES ('TR','RECEIVE','HJXH','13','OUT')")
    db.execute("INSERT INTO ywzt_ods.scm_transfer_order_detail VALUES ('TR','S2')")
    db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_allocate_wms VALUES ('OUT','HJXH','S2',3,'false','SEND'),('OUT','HJXH','S2',999,'true','SEND')")
    yield db
    db.close()


def execute(db, sql):
    tree=sqlglot.parse_one(sql,read='doris')
    for node in list(tree.find_all(exp.DateSub)):
        assert node.args['unit'].name.upper() == 'DAY'
        days=int(node.expression.this)
        node.replace(sqlglot.parse_one(f"DATETIME(NOW(), '-{days} days')",read='sqlite'))
    translated=tree.sql(dialect='sqlite')
    return [dict(row) for row in db.execute(translated)]


def test_formula_groups_before_lock_choice_and_clamps_each_key(inventory_db):
    rows=execute(inventory_db,(ROOT/TEMPLATE).read_text(encoding='utf-8'))
    keyed={r['sku_code']:r for r in rows}
    assert keyed['S1']['available_quantity'] == 90
    assert keyed['S2']['available_quantity'] == 0
    assert keyed['S2']['in_transit_quantity'] == 5
    assert keyed['S3']['available_quantity'] == 43
    assert keyed['TRANSIT_ONLY']['available_quantity'] == 0
    assert keyed['TRANSIT_ONLY']['in_transit_quantity'] == 11
    assert len(rows)==4


def test_receiving_warehouse_scope_does_not_drop_sending_allocation(inventory_db):
    query=bind_current_inventory('SELECT i.* FROM inventory_current i')
    query=apply_scope(query,QueryScope(owner='HJXH',warehouse_codes=('RECEIVE',),sku_codes=('S2',)),legacy_owner='HJXH')
    rows=execute(inventory_db,query)
    assert len(rows)==1
    assert rows[0]['transfer_in_transit']==3
    assert rows[0]['__inventory_base_source_rows']==5
    physical={t.name for t in sqlglot.parse_one(query,read='doris').find_all(exp.Table) if t.db}
    assert 'sku_inventory' not in physical


def test_empty_stock_source_does_not_become_confirmed_zero(inventory_db):
    inventory_db.execute('DELETE FROM ywzt_ods.scm_warehous_manage')
    rows=execute(inventory_db,bind_current_inventory('SELECT i.* FROM inventory_current i'))
    response={'status':'succeeded','execution':{'columns':list(rows[0]),'rows':rows,'row_count':len(rows),'truncated':False,'is_mock':False,'executor_name':'doris'}}
    session=QueryExecutionSession(None,QueryScope(), 'HJXH')
    with pytest.raises(SemanticRuntimeError,match='库存来源没有可核验记录'):
        session._record_response(response,business=True)


def test_inventory_template_does_not_adopt_reference_sales_denominator():
    sql=(ROOT/TEMPLATE).read_text(encoding='utf-8')
    assert 'sumq' not in sql and 'JYCK' not in sql and 'shipQty' not in sql
    assert 'scm_sales_shipment_allocate_wms' in sql  # Transfer transit only.
