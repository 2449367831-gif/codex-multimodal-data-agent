"""Classify independent synthetic facts before consulting live quantities."""

from datetime import date
from decimal import Decimal

import pytest
import sqlglot

from app.tests.test_sales_aggregation import facts
from app.agent_core.channel_breakdown import compile_channel_breakdown, reconcile_channels
from app.query_scope import QueryScope


@pytest.fixture
def channel_facts(facts):
    facts.execute("ALTER TABLE ywzt_ods.scm_sales_shipment_header ADD COLUMN store_code TEXT")
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code='C1'")
    facts.execute("CREATE TABLE ywzt_ods.master_store (company_code TEXT, code TEXT, type TEXT, platform_code TEXT, group1 TEXT, price_group TEXT)")
    facts.executemany("INSERT INTO ywzt_ods.master_store VALUES (?,?,?,?,?,?)", [
        ('HJXH','C1','Z004','DY',None,None),
        ('HJXH','C1','Z004','DY',None,None),
        ('B','C1','Z001',None,'03','122'),
    ])
    return facts


def execute(db, policy='platform', scope=QueryScope(), start=date(2026,9,9), end=date(2026,9,10)):
    sql=compile_channel_breakdown(start=start,end=end,scope=scope,overlap_policy=policy)
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


@pytest.mark.parametrize('policy,label', [('platform','社区团购平台'),('distribution','社区团购分销'),('separate','平台与分销重叠')])
def test_exclusive_overlap_policy_and_duplicate_customer_dedup(channel_facts,policy,label):
    rows=execute(channel_facts,policy)
    assert {(r['customer_channel'],r['channel_detail'],r['quantity']) for r in rows} == {('线上','抖音',8),('线下',label,10)}
    assert sum(r['quantity'] for r in rows)==18
    assert all(r['overlap_quantity']==10 and r['overlap_documents']==1 for r in rows)
    result=reconcile_channels(rows,overlap_policy=policy)
    assert sum(Decimal(r['quantity']) for r in result)==18


@pytest.mark.parametrize('platform,label', [(None,'线上其他'),('','线上其他'),('NEW','线上其他'),('WXSPHXD','抖音'),('DYXD','抖音小时达'),('PDDDF','拼多多系'),('KS','快手系'),('MAOCHAO','淘宝系'),('JD_MARKET','京东系'),('WPH','唯品会')])
def test_online_platforms_and_nulls_are_a_partition(channel_facts,platform,label):
    channel_facts.execute("UPDATE ywzt_ods.master_store SET platform_code=? WHERE company_code='HJXH'",(platform,))
    rows=execute(channel_facts)
    online=next(r for r in rows if r['customer_channel']=='线上')
    assert online['channel_detail']==label and online['quantity']==8
    assert online['channel_quantity']==8
    assert online['unknown_platform_quantity']==(8 if label=='线上其他' and platform else 0)
    assert online['blank_platform_quantity']==(8 if not platform else 0)
    reconcile_channels(rows,overlap_policy='platform')


@pytest.mark.parametrize('group,price,label', [(None,None,'线下其他'),('03',None,'社区团购平台'),(None,'122','社区团购分销'),('x','y','线下其他')])
def test_nullable_offline_attributes_do_not_drop_quantity(channel_facts,group,price,label):
    channel_facts.execute("UPDATE ywzt_ods.master_store SET group1=?,price_group=? WHERE company_code='B'",(group,price))
    rows=execute(channel_facts)
    assert next(r for r in rows if r['customer_channel']=='线下')['channel_detail']==label
    assert sum(r['quantity'] for r in rows)==18


@pytest.mark.parametrize('missing', ['absent','blank_key','null_type','other_type'])
def test_missing_or_other_customers_are_retained(channel_facts,missing):
    if missing=='absent':
        channel_facts.execute("DELETE FROM ywzt_ods.master_store WHERE company_code='B'")
    elif missing=='blank_key':
        channel_facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code='' WHERE company_code='B'")
        channel_facts.execute("UPDATE ywzt_ods.master_store SET code='' WHERE company_code='B'")
    else:
        channel_facts.execute("UPDATE ywzt_ods.master_store SET type=? WHERE company_code='B'",(None if missing=='null_type' else 'UNKNOWN',))
    rows=execute(channel_facts)
    other=next(r for r in rows if r['customer_channel']=='其他')
    assert other['quantity']==10 and other['channel_detail']=='其他'
    assert other['missing_customer_quantity']==(0 if missing=='other_type' else 10)
    reconcile_channels(rows,overlap_policy='platform')


def test_conflicting_master_tuples_never_choose_a_fabricated_min_tuple(channel_facts):
    channel_facts.execute("INSERT INTO ywzt_ods.master_store VALUES ('HJXH','C1','Z004','PDD',NULL,NULL)")
    rows=execute(channel_facts)
    assert sum(r['quantity'] for r in rows)==18
    assert all(r['customer_conflict_count']>0 for r in rows)
    with pytest.raises(ValueError,match='conflict'):
        reconcile_channels(rows,overlap_policy='platform')


def test_unrelated_customer_conflict_does_not_block_selected_owner(channel_facts):
    channel_facts.execute("INSERT INTO ywzt_ods.master_store VALUES ('B','C1','Z004','PDD',NULL,NULL)")
    rows=execute(channel_facts,scope=QueryScope(owner='HJXH'))
    assert len(rows)==1 and rows[0]['quantity']==8
    reconcile_channels(rows,overlap_policy='platform')


def test_scope_reset_exact_sku_and_all_time(channel_facts):
    first=execute(channel_facts,scope=QueryScope(owner='B',sku_codes=('A',)))
    second=execute(channel_facts,start=None,end=None)
    assert sum(r['quantity'] for r in first)==10
    assert sum(r['quantity'] for r in second)==1118
    reconcile_channels(second,overlap_policy='platform')


def test_disjoint_document_counts_include_zero_quantity_documents(channel_facts):
    rows=execute(channel_facts)
    assert sum(r['outbound_count'] for r in rows)==4
    assert all(r['total_outbound_count']==4 for r in rows)
    online=next(r for r in rows if r['customer_channel']=='线上')
    assert online['outbound_count']==3


def test_zero_quantity_missing_customer_still_has_anomaly_documents(channel_facts):
    channel_facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code='MISSING' WHERE outbound_code='O2'")
    rows=execute(channel_facts)
    other=next(r for r in rows if r['customer_channel']=='其他')
    assert other['quantity']==0 and other['outbound_count']==1
    assert other['missing_customer_quantity']==0 and other['missing_customer_documents']==1
    reconcile_channels(rows,overlap_policy='platform')


def test_independent_raw_detail_reference_over_all_classification_combinations(channel_facts):
    from collections import defaultdict
    from itertools import product
    db=channel_facts
    for table in ('scm_sales_shipment_header','scm_sales_shipment_detail','scm_sales_order_header','master_store'):
        db.execute('DELETE FROM ywzt_ods.'+table)
    # This reference uses explicit fixture assignments, not production CASE SQL.
    platform_cases=[('DY','抖音'),('PDD','拼多多系'),('JD','京东系'),(None,'线上其他'),('NEW','线上其他')]
    expected={policy:defaultdict(lambda: [Decimal(0),set()]) for policy in ('platform','distribution','separate')}
    combinations=product(('HJXH','B'),('Z004','Z001',None,'UNKNOWN'),platform_cases,(None,'03'),(None,'122'))
    for index,(owner,kind,(platform,online_label),group,price) in enumerate(combinations):
        customer,doc,order='C'+str(index),'O'+str(index),'S'+str(index)
        db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES (?,?,?,'2026-09-09 12:00:00',700,'JYCK',?,'20',0,'false',?)",(owner,doc,'W1',order,customer))
        db.execute("INSERT INTO ywzt_ods.scm_sales_order_header VALUES (?,?,0,'false')",(owner,order))
        db.executemany("INSERT INTO ywzt_ods.master_store VALUES (?,?,?,?,?,?)",[(owner,customer,kind,platform,group,price)]*2)
        db.executemany("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES (?,?,?,?,0,'false')",[(owner,doc,'A',1.25),(owner,doc,'A',2.5),(owner,doc,'B',0.25)])
        for policy in expected:
            channel='线上' if kind=='Z004' else '线下' if kind=='Z001' else '其他'
            if kind=='Z004':
                detail=online_label
            elif kind!='Z001':
                detail='其他'
            elif group=='03' and price=='122':
                detail={'platform':'社区团购平台','distribution':'社区团购分销','separate':'平台与分销重叠'}[policy]
            elif group=='03':
                detail='社区团购平台'
            elif price=='122':
                detail='社区团购分销'
            else:
                detail='线下其他'
            expected[policy][(channel,detail)][0]+=Decimal('4')
            expected[policy][(channel,detail)][1].add((owner,doc))
    for policy,groups in expected.items():
        actual=reconcile_channels(execute(db,policy),overlap_policy=policy)
        assert {(r['customer_channel'],r['channel_detail']):(Decimal(r['quantity']),r['outbound_count']) for r in actual} == {key:(qty,len(docs)) for key,(qty,docs) in groups.items()}


@pytest.mark.parametrize('field,value', [('quantity',100),('channel_quantity',100),('total_quantity',100),('outbound_count',1.5),('channel_outbound_count',100),('total_outbound_count',100),('group_count',100),('overlap_quantity',100),('overlap_documents',100),('customer_conflict_count',1),('header_conflict_count',1),('missing_customer_quantity',100),('missing_customer_documents',100),('blank_platform_quantity',100),('blank_platform_documents',100),('unknown_platform_documents',100),('negative_quantity_count',1)])
def test_corrupt_or_incomplete_controls_are_rejected(channel_facts,field,value):
    rows=execute(channel_facts)
    rows[0][field]=value
    with pytest.raises(ValueError):
        reconcile_channels(rows,overlap_policy='platform')


def test_duplicate_keys_and_invalid_channel_labels_are_rejected(channel_facts):
    rows=execute(channel_facts)
    with pytest.raises(ValueError):
        reconcile_channels([*rows,rows[0]],overlap_policy='platform')
    rows[0]['channel_detail']='not-a-reviewed-category'
    with pytest.raises(ValueError):
        reconcile_channels(rows,overlap_policy='platform')


@pytest.mark.parametrize('policy', [None,'guess','platform;DROP TABLE x'])
def test_no_default_or_injected_overlap_policy(channel_facts,policy):
    with pytest.raises(ValueError):
        execute(channel_facts,policy)


def test_unsupported_measure_and_partial_window_are_not_silently_ignored(channel_facts):
    for scope in (QueryScope(channel='2C'),QueryScope(unit='BOX')):
        with pytest.raises(ValueError):
            execute(channel_facts,scope=scope)
    with pytest.raises(ValueError):
        execute(channel_facts,start=None)
    assert reconcile_channels([],overlap_policy='platform')==[]


def test_platform_families_match_contract_and_never_overlap():
    from pathlib import Path
    import yaml
    from app.agent_core.channel_breakdown import PLATFORMS
    contract=yaml.safe_load((Path(__file__).resolve().parents[2]/'metrics/sales.channel_breakdown.yaml').read_text(encoding='utf-8'))
    assert {name:tuple(codes) for name,codes in contract['filters']['online_platforms'].items()}==PLATFORMS
    codes=[code for group in PLATFORMS.values() for code in group]
    assert len(codes)==len(set(codes))
    assert 'default' not in contract['parameters']['overlap_policy']
    assert contract['parameters']['overlap_policy']['required'] is True


def test_exact_count_checks_reject_missing_diagnostics_even_with_identical_rows(channel_facts):
    rows=execute(channel_facts)
    for row in rows:
        row['overlap_documents']=0
    with pytest.raises(ValueError):
        reconcile_channels(rows,overlap_policy='platform')


def test_whitespace_normalization_deduplicates_identical_tuples(channel_facts):
    channel_facts.execute("INSERT INTO ywzt_ods.master_store VALUES (' HJXH ',' C1 ',' Z004 ',' DY ',' ','')")
    rows=execute(channel_facts)
    assert sum(r['quantity'] for r in rows)==18
    reconcile_channels(rows,overlap_policy='platform')


def test_pending_contract_cannot_become_executable_through_route_registration(monkeypatch):
    from pathlib import Path
    from app.agent_core.catalog import ReleasedMetricCatalog, ROUTE_METRICS
    project=Path(__file__).resolve().parents[2]
    monkeypatch.setattr('app.agent_core.catalog.active_snapshot',lambda root:(project,None))
    monkeypatch.setitem(ROUTE_METRICS,'sales_channel_breakdown','sales.channel_breakdown')
    assert ReleasedMetricCatalog(project).for_route('sales_channel_breakdown') is None


def test_reconciliation_does_not_round_with_decimal_context(channel_facts):
    from decimal import localcontext
    rows=execute(channel_facts)
    with localcontext() as context:
        context.prec=1
        result=reconcile_channels(rows,overlap_policy='platform')
    assert sum(Decimal(row['quantity']) for row in result)==18
