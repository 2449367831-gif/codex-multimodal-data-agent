"""Two-grain directory output must retain request scope and source conflicts."""

from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3

import pytest
import sqlglot

from app.agent_core.handlers.directory import warehouse_type_summary_detail_request
from app.agent_core.handlers.directory import WarehouseTypeShareHandler
from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRuntimeError
from app.query_scope import QueryScope
from app.chat_api import _public_business_table


QUESTION = (
    "查询名称包含沈阳的所有仓库，排除逻辑删除。按货主和仓库编码联合去重，"
    "同名不同编码分别计数，类型未维护的单列。需要两张不同粒度的表："
    "第一张标题为仓库类型汇总，列出仓库类型、仓库数量、数量占比（%）；"
    "第二张标题为仓库清单，完整列出货主编码、仓库编码、仓库名称、仓库类型，"
    "不使用TOP或LIMIT截断清单。再独立核对两张表各类型数量及总数一致，"
    "全部通过后同时展示两张表，不展示对账明细。"
)

SHARE_QUESTION = (
    "查询名称包含沈阳的所有仓库，按货主和仓库编码联合去重，"
    "按仓库类型统计仓库数量和数量占比，同名不同编码分别计数，用中文表格展示。"
)


@pytest.mark.parametrize("keyword", ["沈阳", "广州", "测试'仓%_", "数量占比仓", "2C/2B仓"])
def test_share_keeps_literal_names_and_all_scope(keyword):
    request = RequestPlanner().classify(SHARE_QUESTION.replace("名称包含沈阳", f"名称包含“{keyword}”"))
    assert request and request.route == "warehouse_type_summary"
    assert request.lookup_keyword == keyword
    assert request.scope == QueryScope()


@pytest.mark.parametrize("clause", [
    "不按货主去重", "按名称去重", "占公司所有仓库总数的比例", "名称也要包含广州",
    "排除云仓", "只看停用仓库", "仅限货主A", "数量大于2", "按数量降序", "按省份汇总",
    "不要排除逻辑删除", "按货主数量统计",
])
def test_share_does_not_erase_unimplemented_predicates(clause):
    request = RequestPlanner().classify(SHARE_QUESTION + clause)
    assert request is None


def test_share_runs_independent_counts_and_only_returns_summary(directory):
    with localcontext() as context:
        context.prec = 3
        result = runtime(directory).run_if_supported(SHARE_QUESTION)
    assert result["query_execution"]["query_call_count"] == 2
    assert result["query_plan"]["grain"] == "warehouse_type"
    assert "business_tables" not in result
    table = _public_business_table(result["business_table"], live=True)
    assert table is not None and "EA" not in table["caption"]
    assert {c["key"] for c in table["columns"]} == {"warehouse_type", "warehouse_count", "warehouse_share"}
    rows = {r["warehouse_type"]: r for r in table["rows"]}
    assert rows["云仓（12）"]["warehouse_count"] == "2"
    assert Decimal(rows["云仓（12）"]["warehouse_share"]) == Decimal("66.67")
    assert Decimal(rows["未配置"]["warehouse_share"]) == Decimal("33.33")
    assert "同步鲜度尚不能确认" in result["interaction"]["answer"]


@pytest.mark.parametrize("control_rows", [[], [dict(warehouse_type="12", warehouse_count=3)]])
def test_share_rejects_independent_mismatch(directory, monkeypatch, control_rows):
    original = WarehouseTypeShareHandler._execute
    def corrupt(self, sql, *, purpose):
        return control_rows if purpose.startswith("independent") else original(self, sql, purpose=purpose)
    monkeypatch.setattr(WarehouseTypeShareHandler, "_execute", corrupt)
    with pytest.raises(SemanticRuntimeError, match="独立对账"):
        runtime(directory).run_if_supported(SHARE_QUESTION)


def test_share_still_rejects_node_conflicts_and_empty_does_not_invent_zero(directory):
    directory.execute("INSERT INTO ywzt_ods.master_wms VALUES ('A','W1','沈阳仓','12','20','0')")
    with pytest.raises(SemanticRuntimeError):
        runtime(directory).run_if_supported(SHARE_QUESTION)
    empty = runtime(directory).run_if_supported(SHARE_QUESTION.replace("沈阳", "不存在"))
    assert empty["result_state"] == "no_data"
    assert empty["business_table"]["rows"] == []


@pytest.fixture
def directory(monkeypatch):
    monkeypatch.setattr("app.agent_core.handlers.directory.active_snapshot",
                        lambda _: (Path(__file__).resolve().parents[2], None))
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
        db.execute("CREATE TABLE ywzt_ods.master_wms (cargo_owner_id TEXT, wms_code TEXT, "
                   "wms_desc TEXT, wms_type TEXT, wms_node TEXT, status TEXT)")
        db.executemany("INSERT INTO ywzt_ods.master_wms VALUES (?,?,?,?,?,?)", [
            ("A", "W1", "沈阳仓", "12", "10", "0"),
            ("A", "W1", "沈阳仓", "12", "10", "0"),
            ("B", "W1", "沈阳仓", "12", "10", "0"),
            ("A", "W2", "沈阳仓", None, None, None),
            ("A", "W3", "广州仓", "12", "10", "0"),
            ("A", "W4", "沈阳已删除仓", "12", "10", "1"),
        ])
        yield db


def runtime(db):
    class Client:
        def query(self, *, sql, purpose):
            rows = [dict(row) for row in db.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False,
                    "truncated": False, "rows": rows, "row_count": len(rows)}}
    return SemanticQueryRuntime(query_client=Client())


def test_original_full_request_retains_two_grains_and_exact_identities(directory):
    result = runtime(directory).run_if_supported(QUESTION)
    summary, detail = result["business_tables"]
    assert {(r["cargo_owner"], r["warehouse_code"]) for r in detail["rows"]} == {
        ("A", "W1"), ("A", "W2"), ("B", "W1")}
    assert sum(int(r["warehouse_count"]) for r in summary["rows"]) == 3
    assert any(r["warehouse_type"] == "未配置" for r in detail["rows"])


@pytest.mark.parametrize("clause,owner", [("仅限货主 A", "A"), ("货主编码为 `B`", "B"),
                                       ("HJXH货主", "HJXH")])
def test_explicit_owner_is_not_discarded_or_inherited(clause, owner):
    planner = RequestPlanner()
    request = planner.classify(QUESTION + clause)
    assert request is not None and request.scope.owner == owner
    assert planner.classify(QUESTION).scope == QueryScope()


def test_both_queries_execute_the_same_owner_scope(directory):
    result = runtime(directory).run_if_supported(QUESTION + "仅限货主 B")
    summary, detail = result["business_tables"]
    assert [(r["cargo_owner"], r["warehouse_code"]) for r in detail["rows"]] == [("B", "W1")]
    assert summary["rows"][0]["warehouse_count"] == "1"


def test_explicit_warehouse_code_is_kept(directory):
    result = runtime(directory).run_if_supported(QUESTION + "仅限仓库编码 `W2`")
    assert [(r["cargo_owner"], r["warehouse_code"]) for r in result["business_tables"][1]["rows"]] == [("A", "W2")]


@pytest.mark.parametrize("suffix", [
    "排除仓库甲", "名称也要包含广州", "只看云仓", "只看停用仓库", "按仓库数量降序",
    "仅返回前5个", "附上所属省份", "不要排除逻辑删除", "不按货主去重", "截至2026-09-01",
    "仅限SKU A", "仅限货主A或仓库编码W2", "全部货主且仅限货主A", "货主A和货主B",
])
def test_unimplemented_predicates_never_become_an_all_scope_report(suffix):
    assert warehouse_type_summary_detail_request(QUESTION + suffix) is None
    request = RequestPlanner().classify(QUESTION + suffix)
    assert request is None or request.route != "warehouse_type_summary_detail"


@pytest.mark.parametrize("name,kind,node", [
    ("广州仓", "12", "10"), ("广州仓", "99", "10"), ("沈阳仓", "12", "20"),
    ("沈阳仓", "12", None), (None, "12", "10"),
])
def test_conflicting_mapping_is_checked_before_keyword_filter(directory, name, kind, node):
    directory.execute("INSERT INTO ywzt_ods.master_wms VALUES ('A','W1',?,?,?,'0')", (name, kind, node))
    with pytest.raises(SemanticRuntimeError):
        runtime(directory).run_if_supported(QUESTION)
    assert runtime(directory).run_if_supported(QUESTION + "仅限货主B")["result_state"] == "live_success"


def test_matching_name_cannot_pull_another_owners_nonmatching_key(directory):
    directory.execute("INSERT INTO ywzt_ods.master_wms VALUES ('C','W1','广州仓','99','10','0')")
    result = runtime(directory).run_if_supported(QUESTION)
    assert {r["cargo_owner"] for r in result["business_tables"][1]["rows"]} == {"A", "B"}


def test_directory_rejects_non_directory_execution_scope(directory):
    runner = runtime(directory)
    request = runner.classify(QUESTION)
    for scope in (QueryScope(sku_codes=("A",)), QueryScope(finished_goods=True), QueryScope(channel="2C")):
        with pytest.raises(SemanticRuntimeError):
            runner.execute_plan(replace(request, scope=scope), question=QUESTION)


def test_empty_scope_returns_no_data_not_unfiltered_result(directory):
    result = runtime(directory).run_if_supported(QUESTION + "货主编码为MISSING")
    assert result["result_state"] == "no_data"
    assert all(table["rows"] == [] for table in result["business_tables"])
