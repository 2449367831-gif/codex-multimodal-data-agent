"""Master-data lookup is independent of sales metrics and warehouse aliases."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sqlite3
from zoneinfo import ZoneInfo

import pytest
import sqlglot
from sqlglot import exp

from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRuntimeError
from app.chat_api import _public_business_table
from app.agent_core.handlers.directory import WarehouseTypeCountHandler


@pytest.fixture(autouse=True)
def candidate_directory_contract(monkeypatch):
    # Validate the candidate before publication; production still reads only
    # its activated immutable source contract.
    monkeypatch.setattr("app.agent_core.handlers.directory.active_snapshot", lambda root: (Path(__file__).resolve().parents[2], None))


class DirectoryBackend:
    def __init__(self, rows=None, truncated=False):
        self.rows = rows if rows is not None else [
            {"cargo_owner": "OWNER_A", "warehouse_code": "TEST001", "warehouse_name": "测试沈阳一仓", "warehouse_type": "12", "warehouse_node": "10", "mapping_variants": 1, "matched_count": 2, "source_updated_at": "2026-09-10 10:00:00"},
            {"cargo_owner": "OWNER_B", "warehouse_code": "TEST002", "warehouse_name": "测试沈阳二仓", "warehouse_type": "99", "warehouse_node": None, "mapping_variants": 1, "matched_count": 2, "source_updated_at": "2026-09-10 10:00:00"},
        ]
        self.calls = []
        self.truncated = truncated

    def query(self, *, sql, purpose=None):
        self.calls.append(sql)
        return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "truncated": self.truncated, "row_count": len(self.rows), "rows": self.rows}}


def runtime(backend=None):
    return SemanticQueryRuntime(query_client=backend or DirectoryBackend(), clock=lambda: datetime(2026, 9, 10, 12, tzinfo=ZoneInfo("Asia/Shanghai")))


@pytest.mark.parametrize("keyword", ["沈阳", "杭州", "成都", "测试'仓%_"])
def test_keyword_is_a_literal_not_a_single_warehouse_alias(keyword):
    request = runtime().classify(f'查询名称包含“{keyword}”的所有仓库，返回仓库编码、仓库名称、仓库类型和仓库节点')
    assert request is not None
    assert request.route == "warehouse_directory"
    assert request.lookup_keyword == keyword
    assert request.scope.owner == "ALL"
    assert request.scope.warehouse_codes == ()
    assert request.start is None and request.end is None


def test_lookup_executes_only_master_data_with_complete_reconciled_output():
    backend = DirectoryBackend()
    result = runtime(backend).run_if_supported('查询名称包含“沈阳”的所有仓库，返回仓库编码、仓库名称、仓库类型和仓库节点')
    assert result is not None
    assert len(backend.calls) == 1
    tree = sqlglot.parse_one(backend.calls[0], read="doris")
    sources = {t.sql() for t in tree.find_all(exp.Table) if t.db}
    assert sources == {"ywzt_ods.master_wms"}
    literals = {x.this for x in tree.find_all(exp.Literal)}
    assert "沈阳" in literals and "HJXH" not in literals
    assert "warehouse" == result["query_plan"]["grain"]
    assert result["query_execution"]["live_business_result_executed"]
    public = _public_business_table(result["business_table"], live=True)
    assert public is not None and len(public["rows"]) == 2
    assert {c["key"] for c in public["columns"]} >= {"warehouse_code", "warehouse_name", "warehouse_type", "warehouse_node"}
    assert "99" in public["rows"][1]["warehouse_type"]


def test_unknown_owner_or_status_constraint_is_not_silently_discarded():
    assert runtime().classify('查询名称包含“沈阳”的仓库，仅限测试货主') is None
    assert runtime().classify('查询名称包含“沈阳”的仓库，只看停用仓库') is None


@pytest.mark.parametrize("clause", [
    "按仓库类型统计仓库数", "计算各类型占比", "按仓库编码降序排列",
    "仅返回前5个", "对比各类型数量", "附上所属省份", "名称也要包含广州",
    "按名称去重，同名不同编码合并", "统计仓库数，输出表格",
])
def test_list_route_never_discards_requested_computation_or_projection(clause):
    request = runtime().classify('查询名称包含“沈阳”的仓库，' + clause)
    if clause == "按仓库类型统计仓库数":
        assert request and request.route == "warehouse_type_count"
    else:
        assert request is None


def test_empty_directory_is_successful_no_data_not_a_clarification():
    result = runtime(DirectoryBackend(rows=[])).run_if_supported('查名称包含“测试不存在”的仓库')
    assert result is not None
    assert result["interaction"]["outcome"] == "completed"
    assert result["result_state"] == "no_data"
    assert result["business_table"]["rows"] == []


@pytest.mark.parametrize("reason", ["conflict", "incomplete", "truncated"])
def test_conflicting_or_incomplete_directory_is_not_published(reason):
    backend = DirectoryBackend(truncated=reason == "truncated")
    if reason == "conflict":
        backend.rows[0]["mapping_variants"] = 2
    if reason == "incomplete":
        backend.rows[0]["matched_count"] = 3
    with pytest.raises(SemanticRuntimeError):
        runtime(backend).run_if_supported('查询名称包含“沈阳”的仓库')


def type_count(rows):
    execution = SimpleNamespace(legacy_owner="HJXH", query=lambda sql, **kwargs: rows)
    return WarehouseTypeCountHandler(execution, timezone_name="Asia/Shanghai").run(
        runtime().classify('查询名称包含“沈阳”的仓库，按仓库类型统计仓库数')
    )


def test_type_count_reconciles_distinct_codes_and_maps_missing_type():
    answer = type_count([
        {"warehouse_type": "12", "warehouse_count": "2", "matched_warehouse_count": "3",
         "conflicting_code_count": "0", "blank_code_count": "0", "group_count": "2"},
        {"warehouse_type": None, "warehouse_count": "1", "matched_warehouse_count": "3",
         "conflicting_code_count": "0", "blank_code_count": "0", "group_count": "2"},
    ])
    assert answer.business_table["rows"][0]["warehouse_count"] == "2"
    assert "未配置" in answer.business_table["rows"][1]["warehouse_type"]


@pytest.mark.parametrize("field", ["conflicting_code_count", "blank_code_count"])
def test_type_count_never_publishes_unresolved_source_controls(field):
    rows = [{"warehouse_type": "12", "warehouse_count": "1", "matched_warehouse_count": "1",
             "conflicting_code_count": "0", "blank_code_count": "0", "group_count": "1"}]
    rows[0][field] = "1"
    with pytest.raises(SemanticRuntimeError):
        type_count(rows)


def count_sql_rows(records):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
        db.execute("CREATE TABLE ywzt_ods.master_wms (cargo_owner_id TEXT, wms_code TEXT, wms_desc TEXT, wms_type TEXT, status TEXT)")
        db.executemany("INSERT INTO ywzt_ods.master_wms VALUES (?,?,?,?,?)", records)
        sql = sqlglot.transpile(WarehouseTypeCountHandler._sql("沈阳"), read="doris", write="sqlite")[0]
        return [dict(row) for row in db.execute(sql)]


def test_count_sql_deduplicates_codes_not_names_or_physical_rows():
    record = ("A", "W1", "沈阳仓", "12", "0")
    rows = count_sql_rows([record, record, ("B", "W1", "沈阳仓", "12", "0"),
                          ("A", "W2", "沈阳仓", "12", "0"),
                          ("A", "W3", "沈阳未配置", None, "0"),
                          ("A", "W4", "广州仓", "12", "0"),
                          ("A", "W5", "沈阳停用", "12", "1")])
    assert sum(row["warehouse_count"] for row in rows) == 4
    assert {row["matched_warehouse_count"] for row in rows} == {4}
    assert len(type_count(rows).business_table["rows"]) == 2


@pytest.mark.parametrize("conflict_name", ["沈阳仓", "广州仓"])
def test_type_conflict_is_detected_even_on_nonmatching_name(conflict_name):
    rows = count_sql_rows([("A", "W1", "沈阳仓", "12", "0"),
                          ("A", "W1", conflict_name, "99", "0")])
    assert rows[0]["conflicting_code_count"] == 1
    with pytest.raises(SemanticRuntimeError):
        type_count(rows)


def test_same_code_different_owner_is_counted_as_two_contract_keys():
    rows = count_sql_rows([("A", "W1", "沈阳仓", "12", "0"),
                           ("B", "W1", "沈阳仓", "12", "0")])
    assert rows[0]["matched_warehouse_count"] == 2
    assert rows[0]["warehouse_count"] == 2


def test_name_conflict_is_not_hidden_when_type_matches():
    rows = count_sql_rows([("A", "W1", "沈阳仓", "12", "0"),
                          ("A", "W1", "广州仓", "12", "0")])
    assert rows[0]["conflicting_code_count"] == 1
    with pytest.raises(SemanticRuntimeError):
        type_count(rows)


def test_name_match_cannot_pull_in_another_owners_same_code():
    rows = count_sql_rows([("A", "W1", "沈阳仓", "12", "0"),
                          ("B", "W1", "广州仓", "99", "0")])
    assert len(rows) == 1
    assert rows[0]["matched_warehouse_count"] == 1
    assert rows[0]["warehouse_type"] == "12"


@pytest.mark.parametrize("owner,code", [(None, "W1"), ("A", ""), (" ", "W1")])
def test_incomplete_identity_stops_count(owner, code):
    with pytest.raises(SemanticRuntimeError):
        type_count(count_sql_rows([(owner, code, "沈阳仓", "12", "0")]))


def test_count_empty_result_is_empty_not_a_fabricated_zero_row():
    assert type_count(count_sql_rows([])).business_table["rows"] == []


@pytest.mark.parametrize("keyword", ["沈阳", "杭州", "成都", "测试'仓%_"])
def test_count_preserves_literal_keyword_and_grouping(keyword):
    request = runtime().classify(f'查询名称包含“{keyword}”的仓库，按仓库类型统计仓库数，仓库编码去重，同名不同编码分别计数，用表格展示。')
    assert request and request.route == "warehouse_type_count"
    assert request.lookup_keyword == keyword
    assert request.scope.owner == "ALL"


def test_type_share_composition_uses_reconciled_directory_report():
    request = runtime().classify(
        "查询名称包含沈阳的所有仓库，按货主和仓库编码联合去重，"
        "按仓库类型统计仓库数量和数量占比，同名不同编码分别计数，用中文表格展示。"
    )
    assert request and request.route == "warehouse_type_summary"
    assert request.lookup_keyword == "沈阳"


@pytest.mark.parametrize("suffix", ["只看云仓", "按数量降序", "名称也要包含广州", "按名称去重", "仅限货主A"])
def test_count_preserves_unimplemented_constraints_for_generic_planning(suffix):
    assert runtime().classify('查询名称包含“沈阳”的仓库，按仓库类型统计仓库数，' + suffix) is None
