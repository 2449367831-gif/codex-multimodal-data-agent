from datetime import date, datetime

import pytest

from app.agent_core.planning import RequestPlanner
from app.agent_core.name_quality_planning import name_quality_clarification
from app.query_scope import ScopeResolver
from app.codex_agent import reviewed_clarification_for_question, interaction_envelope
from app.agent_core.runtime import SemanticQueryRuntime
from app.tests.test_sku_name_quality import named_facts
from app.tests.test_sales_aggregation import facts
from app.tests.test_outbound_boxes import client_for
from app.chat_api import _public_business_table

QUESTION='查询 SKU `QY-YWKC500G` 的出库数据。如果订单明细名称、出库明细名称和主数据名称不一致，应该显示哪个名称，并说明处理原则。'


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr('app.agent_core.name_quality_planning.active_name_contract',lambda _:True)


def test_ambiguous_basis_has_one_meaningful_card_and_all_time_scope():
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    assert planner.classify(QUESTION) is None
    card=reviewed_clarification_for_question(QUESTION)
    assert card and len(card['questions'])==1
    assert [o['id'] for o in card['questions'][0]['options']]==['sku_name_actual','sku_name_defer']
    selection=[{'question_id':'sku_name_outbound_basis','option_id':'sku_name_actual'}]
    selected=planner.plan_selected(QUESTION,selection)
    assert selected and selected.start is None and selected.end is None
    assert selected.scope.sku_codes==('QY-YWKC500G',) and selected.scope.owner=='ALL'
    assert planner.plan_selected(QUESTION,[{'question_id':'sku_name_outbound_basis','option_id':'sku_name_defer'}]) is None


def test_explicit_basis_and_date_do_not_prompt_again():
    question=QUESTION.replace('的出库数据','昨天的实际销售出库数据')
    request=RequestPlanner(clock=lambda:datetime(2026,9,10,12)).classify(question)
    assert request and request.start==date(2026,9,9) and request.end==date(2026,9,10)
    assert reviewed_clarification_for_question(question) is None


@pytest.mark.parametrize('suffix',['仅限客户甲','排除云仓','按吨返回','仅限状态700','同时计算分配数量','按仓库分行','状态为700','同时查询库存'])
def test_unknown_predicates_do_not_reach_name_route(suffix):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    request=planner.classify(QUESTION.replace('的出库数据','昨天的实际销售出库数据')+suffix)
    assert request is None or request.route!='sales_sku_name_quality'


def test_runtime_names_keep_raw_quantity_empty_values_and_notes(named_facts):
    runtime=SemanticQueryRuntime(query_client=client_for(named_facts),clock=lambda:datetime(2026,9,10,12))
    question=QUESTION.replace('`QY-YWKC500G`','`A`').replace('的出库数据','昨天的实际销售出库数据')
    result=runtime.run_if_supported(question)
    assert result and result['result_state']=='live_success'
    table=result['business_table']
    assert len(table['columns'])==12 and len(table['rows'])==2 and len(table['notes'])==3
    assert {r['sku_code'] for r in table['rows']}=={'A'}
    public=_public_business_table(table,live=True)
    assert public and public['notes']==table['notes']


def test_all_time_is_not_a_hidden_recent_window(named_facts):
    runtime=SemanticQueryRuntime(query_client=client_for(named_facts),clock=lambda:datetime(2026,9,10,12))
    question=QUESTION.replace('`QY-YWKC500G`','`A`').replace('的出库数据','的实际销售出库数据')
    result=runtime.run_if_supported(question)
    assert result['query_plan']['start'] is None and '全部时间' in result['business_table']['caption']
    assert sum(float(r['quantity']) for r in result['business_table']['rows'])==1113.75


def test_independent_name_reference_matches_synthetic_facts(named_facts):
    import runpy
    from pathlib import Path
    import sqlglot
    script=runpy.run_path(str(Path(__file__).resolve().parents[2]/'tests/acceptance/check_sku_names_live.py'))
    for table in ('scm_sales_shipment_detail','scm_sales_order_detail','master_sku'):
        named_facts.execute(f"UPDATE ywzt_ods.{table} SET sku_code='QY-YWKC500G' WHERE sku_code='A'")
    runtime=SemanticQueryRuntime(query_client=client_for(named_facts),clock=lambda:datetime(2026,9,10,12))
    result=runtime.run_if_supported(QUESTION.replace('的出库数据','的实际销售出库数据'))
    observations=[dict(row) for row in named_facts.execute(sqlglot.transpile(script['reference_sql'](),read='doris',write='sqlite')[0])]
    assert all(script['compare'](result['business_table'],observations).values())
