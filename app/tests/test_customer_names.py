"""Customer-label policy tests use synthetic identities, never live answers."""

from pathlib import Path
import sqlite3

import pytest
import sqlglot

from app.agent_core.customer_names import CustomerNamePolicy, compile_customer_names, reconcile_customer_names


EXCEPTIONS=('185216','181898','181930','184856','180961','180962','180967','180965','183507','181897','180966','180462')


@pytest.fixture
def policy():
    return CustomerNamePolicy('2011','地配快递',EXCEPTIONS)


@pytest.fixture
def customers():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.execute("CREATE TABLE ywzt_ods.master_store(company_code TEXT,code TEXT,name TEXT,price_group TEXT,updated TEXT DEFAULT '2026-09-10 08:00:00')")
    db.executemany('INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES (?,?,?,?)',[
        ('A','ORDINARY','原始甲','2011'),('A','ORDINARY','原始甲','2011'),
        ('B','ORDINARY','原始乙','OTHER'),('A','185216','例外甲','2011'),
        ('A','NULL_PRICE','价格组未配',None),('A','NULL_NAME',None,'2011'),
        ('A','SAME_NAME','原始甲','OTHER'),
    ])
    yield db
    db.close()


def execute(db,policy,**kwargs):
    sql=compile_customer_names(policy,**kwargs)
    return [dict(r) for r in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


def test_merge_does_not_change_identity_original_name_or_exception(customers,policy):
    rows=execute(customers,policy)
    result=reconcile_customer_names(rows,policy)
    by_key={(r['cargo_owner'],r['customer_code']):r for r in result}
    normal=by_key[('A','ORDINARY')]
    assert normal['display_name']=='地配快递' and normal['original_name']=='原始甲'
    assert normal['merge_applied']==1 and normal['source_record_count']==2
    assert by_key[('A','185216')]['display_name']=='例外甲'
    assert by_key[('A','185216')]['merge_applied']==0
    assert by_key[('B','ORDINARY')]['display_name']=='原始乙'
    assert by_key[('A','SAME_NAME')]['display_name']=='原始甲'
    assert len(result)==6


@pytest.mark.parametrize('code',EXCEPTIONS)
def test_every_explicit_exception_keeps_original_name(customers,policy,code):
    customers.execute('DELETE FROM ywzt_ods.master_store')
    customers.execute('INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES (?,?,?,?)',('A',code,'必须保留','2011'))
    row=reconcile_customer_names(execute(customers,policy),policy)[0]
    assert row['display_name']=='必须保留' and row['is_exception']==1 and row['merge_applied']==0


def test_exception_matching_is_by_exact_code_not_name_or_numeric_cast(customers,policy):
    customers.executemany('INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES (?,?,?,?)',[
        ('A','0185216','必须合并','2011'),('A','OTHER_CODE','185216','2011'),
        ('A','185216-X','例外甲','2011'),
    ])
    rows=reconcile_customer_names(execute(customers,policy),policy)
    assert all(r['merge_applied']==1 for r in rows if r['customer_code'] in ('0185216','OTHER_CODE','185216-X'))


def test_null_price_group_retains_name_and_null_name_keeps_missing_flag(customers,policy):
    rows={r['customer_code']:r for r in reconcile_customer_names(execute(customers,policy,owners=('A',)),policy)}
    assert rows['NULL_PRICE']['display_name']=='价格组未配' and rows['NULL_PRICE']['merge_applied']==0
    assert rows['NULL_NAME']['display_name']=='地配快递' and rows['NULL_NAME']['missing_name_rows']==1
    assert rows['NULL_PRICE']['missing_price_rows']==1


def test_unmatched_requested_customer_is_preserved_and_not_fabricated(customers,policy):
    rows=reconcile_customer_names(execute(customers,policy,customer_keys=(('A','MISSING'),('B','185216'))),policy)
    assert len(rows)==2
    assert all(r['source_record_count']==0 and r['missing_master']==1 and r['display_name'] is None and r['merge_applied']==0 for r in rows)


def test_conflicting_price_groups_are_reported_without_arbitrary_merge(customers,policy):
    customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES ('A','ORDINARY','原始甲','OTHER')")
    row=next(r for r in reconcile_customer_names(execute(customers,policy),policy) if (r['cargo_owner'],r['customer_code'])==('A','ORDINARY'))
    assert row['price_variants']==2 and row['merge_applied']==0
    assert row['display_name']=='原始甲' and row['price_group'] is None


def test_missing_and_present_price_is_conflict_not_min_fallback(customers,policy):
    customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES ('A','ORDINARY','原始甲',NULL)")
    row=next(r for r in reconcile_customer_names(execute(customers,policy),policy) if r['cargo_owner']=='A' and r['customer_code']=='ORDINARY')
    assert row['price_variants']==2 and row['merge_applied']==0


def test_conflicting_names_stay_diagnostic_even_if_merge_label_is_unique(customers,policy):
    customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES ('A','ORDINARY','冲突名称','2011')")
    rows=reconcile_customer_names(execute(customers,policy),policy)
    row=next(r for r in rows if r['cargo_owner']=='A' and r['customer_code']=='ORDINARY')
    assert row['original_name'] is None and row['name_variants']==2 and row['display_name']=='地配快递'
    customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES ('A','185216','冲突例外名称','2011')")
    row=next(r for r in reconcile_customer_names(execute(customers,policy),policy) if r['customer_code']=='185216')
    assert row['display_name'] is None and row['name_variants']==2 and row['merge_applied']==0


def test_whitespace_dedup_and_owner_scope_do_not_filter_unmentioned_owners(customers,policy):
    customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES (' A ',' ORDINARY ',' 原始甲 ',' 2011 ')")
    selected=reconcile_customer_names(execute(customers,policy,owners=('A',)),policy)
    all_rows=reconcile_customer_names(execute(customers,policy),policy)
    assert {r['cargo_owner'] for r in selected}=={'A'}
    assert {r['cargo_owner'] for r in all_rows}=={'A','B'}
    assert next(r for r in selected if r['customer_code']=='ORDINARY')['source_record_count']==3


def test_blank_source_keys_do_not_silently_disappear(customers,policy):
    customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES ('A','','无法识别','2011')")
    with pytest.raises(ValueError,match='key'):
        reconcile_customer_names(execute(customers,policy),policy)


@pytest.mark.parametrize('field,value', [('group_count',100),('total_source_records',100),('source_record_count',-1),('name_variants',100),('missing_name_rows',100),('price_variants',100),('missing_price_rows',100),('is_exception',1),('merge_applied',1),('missing_master',1),('display_name','伪造名称')])
def test_corrupted_controls_or_name_transform_are_rejected(customers,policy,field,value):
    rows=execute(customers,policy,customer_keys=(('B','ORDINARY'),))
    rows[0][field]=value
    with pytest.raises(ValueError):
        reconcile_customer_names(rows,policy)


def test_duplicate_or_truncated_groups_are_not_complete(customers,policy):
    rows=execute(customers,policy)
    with pytest.raises(ValueError):
        reconcile_customer_names([*rows,rows[0]],policy)
    with pytest.raises(ValueError):
        reconcile_customer_names(rows[:-1],policy)


def test_keys_are_sql_literals_and_not_queries(customers,policy):
    malicious="x' OR 1=1 --"
    row=reconcile_customer_names(execute(customers,policy,customer_keys=(('A',malicious),)),policy)[0]
    assert row['customer_code']==malicious and row['source_record_count']==0


def test_no_rows_is_not_a_fake_customer(customers,policy):
    customers.execute('DELETE FROM ywzt_ods.master_store')
    assert reconcile_customer_names(execute(customers,policy),policy)==[]


@pytest.mark.parametrize('kwargs',[{'owners':('A',),'customer_keys':(('B','C'),)}, {'owners':('',)}, {'customer_keys':(('', 'C'),)}, {'customer_keys':(('A',''),)}])
def test_invalid_or_conflicting_scope_is_rejected(policy,kwargs):
    with pytest.raises(ValueError):
        compile_customer_names(policy,**kwargs)


def test_contract_preserves_all_twelve_exact_exception_codes(policy):
    import yaml
    contract=yaml.safe_load((Path(__file__).resolve().parents[2]/'metrics/customer.display_name.yaml').read_text(encoding='utf-8'))
    assert CustomerNamePolicy.from_contract(contract)==policy
    assert contract['status']=='candidate'


def test_only_invalid_source_keys_is_not_misreported_as_no_data(customers,policy):
    customers.execute('DELETE FROM ywzt_ods.master_store')
    customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES (NULL,'CODE','缺少货主','2011')")
    with pytest.raises(ValueError,match='key'):
        reconcile_customer_names(execute(customers,policy),policy)


@pytest.mark.parametrize('arguments', [('2011','地配快递',()),('2011','地配快递',('A','A')),('2011','地配快递',('',)),(' 2011','地配快递',('A',))])
def test_invalid_policy_cannot_change_merge_behavior(arguments):
    with pytest.raises(ValueError):
        CustomerNamePolicy(*arguments)


def test_complete_pages_reconcile_all_keys_and_detect_changed_names(customers,policy):
    from app.agent_core.customer_names import retrieve_customer_names
    from app.agent_core.semantic_types import SemanticRuntimeError
    calls=[]
    def query(sql,*,purpose):
        calls.append(sql)
        return [dict(r) for r in customers.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]
    rows=retrieve_customer_names(policy,query,page_size=2)
    assert len(rows)==6 and len(calls)==6
    calls.clear()
    def changed(sql,*,purpose):
        if len(calls)==3:
            customers.execute("UPDATE ywzt_ods.master_store SET name='查询期间改名' WHERE code='185216'")
        return query(sql,purpose=purpose)
    with pytest.raises(SemanticRuntimeError,match='变化'):
        retrieve_customer_names(policy,changed,page_size=2)


@pytest.mark.parametrize('mode',['capacity','bytes','missing_page','blank_identity'])
def test_incomplete_customer_directory_is_never_success(customers,policy,mode):
    from app.agent_core.customer_names import retrieve_customer_names
    from app.agent_core.semantic_types import SemanticRuntimeError
    calls=[]
    if mode=='blank_identity':
        customers.execute("INSERT INTO ywzt_ods.master_store(company_code,code,name,price_group) VALUES ('A',NULL,'坏编码','2011')")
    def query(sql,*,purpose):
        calls.append(sql)
        result=[dict(r) for r in customers.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]
        return [] if mode=='missing_page' and len(calls)==2 else result
    with pytest.raises(SemanticRuntimeError) as error:
        retrieve_customer_names(policy,query,page_size=2,max_rows=3 if mode=='capacity' else 100000,max_bytes=1 if mode=='bytes' else 64*1024*1024)
    assert error.value.reason=={'capacity':'result_truncated','bytes':'result_truncated','missing_page':'customer_page_invalid','blank_identity':'customer_identity_invalid'}[mode]


def test_missing_freshness_is_explicit_and_never_fabricated(customers,policy):
    customers.execute('UPDATE ywzt_ods.master_store SET updated=NULL')
    rows=reconcile_customer_names(execute(customers,policy),policy)
    assert all(r['source_latest_updated'] is None and r['missing_update_rows']==7 for r in rows)


@pytest.mark.parametrize('field,value',[('missing_update_rows',100),('source_latest_updated',None),('source_latest_updated','not-a-date')])
def test_corrupt_freshness_is_rejected(customers,policy,field,value):
    rows=execute(customers,policy)
    rows[0][field]=value
    with pytest.raises(ValueError):
        reconcile_customer_names(rows,policy)


def test_independent_live_reference_is_first_checked_against_synthetic_facts(customers,policy):
    import importlib.util
    spec=importlib.util.spec_from_file_location('customer_name_reference',Path(__file__).resolve().parents[2]/'tests/acceptance/check_customer_names_live.py')
    reference=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    observations=[dict(row) for row in customers.execute(sqlglot.transpile(reference.reference_sql(),read='doris',write='sqlite')[0])]
    actual=reconcile_customer_names(execute(customers,policy),policy)
    assert all(reference.compare_labels(actual,reference.expected_labels(observations,policy)).values())
