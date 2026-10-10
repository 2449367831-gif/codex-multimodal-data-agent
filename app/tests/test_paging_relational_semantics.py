import sqlite3

import pytest
import sqlglot

from app.agent_core.sales_pages import aggregate_page_sql, retrieve_sales_groups


@pytest.fixture
def facts():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE facts(sku TEXT,quantity INTEGER)')
    db.executemany('INSERT INTO facts VALUES (?,?)',[('B',2),('A',1),('B',2),('C',3),('A',1)])
    yield db
    db.close()


def read(db,sql):
    return [dict(row) for row in db.execute(sqlglot.transpile(sql,read='doris',write='sqlite')[0])]


@pytest.mark.parametrize('size',[1,2,500])
def test_numbering_happens_after_distinct_not_before(facts,size):
    sql='SELECT DISTINCT sku,quantity,3 AS group_count FROM facts ORDER BY sku LIMIT 501'
    actual=retrieve_sales_groups(sql,lambda sql,**_:read(facts,sql),page_size=size)
    assert [(row['sku'],row['quantity']) for row in actual]==[('A',1),('B',2),('C',3)]
    assert [row['result_ordinal'] for row in actual]==[1,2,3]


@pytest.mark.parametrize('ordering',['quantity DESC,sku','QUANTITY DESC,SKU','SUM(quantity) DESC,sku','SUM(QUANTITY) DESC,SKU','2 DESC,1'])
def test_aggregate_alias_and_select_positions_keep_original_order(facts,ordering):
    sql=f'SELECT sku,SUM(quantity) AS quantity,3 AS group_count FROM facts GROUP BY sku ORDER BY {ordering} LIMIT 501'
    expected=read(facts,sql)
    actual=retrieve_sales_groups(sql,lambda sql,**_:read(facts,sql),page_size=1)
    assert [{key:value for key,value in row.items() if key!='result_ordinal'} for row in actual]==expected


@pytest.mark.parametrize('ordering',['f.sku DESC','f.SKU DESC','code DESC','CODE DESC'])
def test_renamed_output_key_keeps_qualified_source_order(facts,ordering):
    sql=f'SELECT DISTINCT f.sku AS code,3 AS group_count FROM facts f ORDER BY {ordering} LIMIT 501'
    actual=retrieve_sales_groups(sql,lambda sql,**_:read(facts,sql),page_size=2)
    assert [row['code'] for row in actual]==['C','B','A']


def test_window_output_can_be_used_as_final_order(facts):
    sql='SELECT sku,SUM(quantity) AS quantity,RANK() OVER(ORDER BY SUM(quantity) DESC) AS rank_value,3 AS group_count FROM facts GROUP BY sku ORDER BY rank_value,sku LIMIT 501'
    expected=read(facts,sql)
    actual=retrieve_sales_groups(sql,lambda sql,**_:read(facts,sql),page_size=2)
    assert [{key:value for key,value in row.items() if key!='result_ordinal'} for row in actual]==expected


@pytest.mark.parametrize('sql',[
    'SELECT sku FROM facts ORDER BY quantity',
    'SELECT * FROM facts ORDER BY 2',
    'SELECT sku FROM facts ORDER BY 0',
    'SELECT sku FROM facts ORDER BY 2',
    'SELECT sku AS result_ordinal FROM facts ORDER BY result_ordinal',
    'SELECT sku FROM facts ORDER BY sku; SELECT quantity FROM facts ORDER BY quantity',
])
def test_unresolvable_order_or_reserved_ordinal_is_not_guessed(sql):
    with pytest.raises(ValueError):aggregate_page_sql(sql,0,2)


@pytest.mark.parametrize('direction',['ASC NULLS FIRST','DESC NULLS LAST'])
def test_null_ordering_survives_result_boundary(facts,direction):
    facts.execute('INSERT INTO facts VALUES (NULL,4)')
    sql=f'SELECT DISTINCT sku,4 AS group_count FROM facts ORDER BY sku {direction} LIMIT 501'
    expected=read(facts,sql)
    actual=retrieve_sales_groups(sql,lambda sql,**_:read(facts,sql),page_size=2)
    assert [{key:value for key,value in row.items() if key!='result_ordinal'} for row in actual]==expected


def test_union_deduplication_finishes_before_numbering(facts):
    sql="SELECT sku,3 AS group_count FROM facts WHERE sku<>'C' UNION SELECT sku,3 AS group_count FROM facts WHERE sku<>'A' ORDER BY sku LIMIT 501"
    actual=retrieve_sales_groups(sql,lambda sql,**_:read(facts,sql),page_size=1)
    assert [row['sku'] for row in actual]==['A','B','C']
