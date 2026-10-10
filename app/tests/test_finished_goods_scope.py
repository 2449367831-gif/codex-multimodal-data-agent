"""Finished-goods union semantics before implementation or live verification."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlglot
import yaml

from app.tests.test_sales_aggregation import facts
from app.query_scope import QueryScope, apply_scope
from app.agent_core.planning import RequestPlanner
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.sales_aggregation import SalesAggregation, compile_sales_aggregation
from app.tests.test_metric_catalog import catalog_root, write_metric, write_release


EXCEPTIONS = ("5101010010008", "5101010010009")
QUESTION = "查询昨天所有货主的成品销售出库，成品定义为 `master_sku.sku_group = '50'`，同时保留 SKU `5101010010008` 和 `5101010010009` 两个例外。按 SKU 汇总实际出库量。"


@pytest.fixture(autouse=True)
def candidate_contracts(monkeypatch):
    candidate = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("app.query_scope.active_snapshot", lambda _: (candidate, None))
    monkeypatch.setattr("app.agent_core.planning.active_snapshot", lambda _: (candidate, None))


def compile_rows(db, scope):
    sql = apply_scope(compile_sales_aggregation(SalesAggregation(("sku",)), start=date(2026, 9, 9), end=date(2026, 9, 10)), scope, legacy_owner="HJXH")
    return [dict(row) for row in db.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]


def add_exceptions(db):
    db.executemany("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES ('HJXH','O1',?,?,0,'false')", [(EXCEPTIONS[0], "2"), (EXCEPTIONS[1], "3")])
    db.executemany("INSERT INTO ywzt_ods.master_sku VALUES (?,?,?)", [(EXCEPTIONS[0], "Synthetic exception", "60"), ("A", "Synthetic duplicate", "50"), ("A", "Synthetic duplicate", "50")])


def test_definition_or_exceptions_does_not_filter_out_nonfinished_or_missing_master(facts):
    add_exceptions(facts)
    rows = compile_rows(facts, QueryScope(finished_goods=True, finished_goods_exceptions=EXCEPTIONS))
    assert {row["sku_code"] for row in rows} == {"A", *EXCEPTIONS}
    assert sum(Decimal(str(row["quantity"])) for row in rows) == Decimal("18.75")
    assert {row["total_outbound_count"] for row in rows} == {4}


def test_exceptions_do_not_leak_to_the_next_plain_finished_goods_request(facts):
    add_exceptions(facts)
    assert {row["sku_code"] for row in compile_rows(facts, QueryScope(finished_goods=True))} == {"A"}
    assert {row["sku_code"] for row in compile_rows(facts, QueryScope())} == {"A", "B", *EXCEPTIONS}


def test_exact_requirement_is_sku_aggregate_not_a_sku_filter_or_total():
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 10, 12))
    request = planner.classify(QUESTION)
    assert request and request.route == "sales_aggregate"
    assert request.sales_aggregation.dimensions == ("sku",)
    assert request.scope.owner == "ALL" and request.scope.sku_codes == ()
    assert request.scope.finished_goods_exceptions == EXCEPTIONS
    assert request.scope.finished_goods
    assert request.start == date(2026, 9, 9) and request.end == date(2026, 9, 10)
    plain = planner.classify("昨天成品实际销售出库，按SKU汇总数量")
    assert plain.sales_aggregation.dimensions == ("sku",) and plain.scope.finished_goods_exceptions == ()


@pytest.mark.parametrize("changed", [
    QUESTION.replace("'50'", "'60'"),
    QUESTION.replace(EXCEPTIONS[1], "UNREVIEWED-SKU"),
    QUESTION + "只限客户甲", QUESTION + "按吨返回", QUESTION + "排除HJXH",
    QUESTION.replace("按 SKU", "按仓库"),
    "当前成品库存，同时保留SKU 5101010010008 和 5101010010009 两个例外",
])
def test_unreviewed_or_contradictory_conditions_are_not_dropped(changed):
    assert RequestPlanner().classify(changed) is None


def test_exception_scope_cannot_exist_without_finished_goods():
    with pytest.raises(ValueError):
        QueryScope(finished_goods_exceptions=EXCEPTIONS)


def test_runtime_keeps_total_and_exception_notes_with_public_table(facts):
    from app.chat_api import _public_business_table
    add_exceptions(facts)
    class Client:
        def query(self, *, sql, purpose):
            cursor = facts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])
            columns = [column[0] for column in cursor.description]
            rows = [dict(row) for row in cursor]
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}}
    runtime = SemanticQueryRuntime(query_client=Client(), clock=lambda: datetime(2026, 9, 10, 12))
    result = runtime.run_if_supported(QUESTION)
    assert result is not None and result["result_state"] == "live_success"
    table = _public_business_table(result["business_table"], live=True)
    assert {r["sku_code"] for r in table["rows"]} == {"A", *EXCEPTIONS}
    assert all(code in "".join(table["notes"]) for code in EXCEPTIONS)


def test_conflicting_nonexception_master_groups_block_public_result(facts):
    from app.agent_core.semantic_types import SemanticRuntimeError
    add_exceptions(facts)
    facts.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A','Conflicting classification','60')")
    class Client:
        def query(self, *, sql, purpose):
            cursor = facts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])
            columns = [column[0] for column in cursor.description]
            rows = [dict(row) for row in cursor]
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}}
    runtime = SemanticQueryRuntime(query_client=Client(), clock=lambda: datetime(2026, 9, 10, 12))
    with pytest.raises(SemanticRuntimeError) as failure:
        runtime.run_if_supported(QUESTION)
    assert failure.value.reason == "finished_goods_classification_conflict"


def test_scope_word_matching_does_not_create_new_words_by_deletion():
    from app.query_scope import ScopeResolver
    assert ScopeResolver().resolve("昨天成品销售出库按SKU汇总实际出库量")[2] is False
    assert ScopeResolver().resolve("昨天成品销售出库按SKU汇总实际出库量，仅限客户甲")[2] is True


def test_core_records_the_explicit_metric_and_preserves_membership_scope(facts, monkeypatch, catalog_root):
    from app.agent_core import AgentOrchestrator
    candidate = Path(__file__).resolve().parents[2]
    path = write_metric(catalog_root, "exceptions.yaml", "sales.finished_goods_exceptions", "1.0.0")
    metric = yaml.safe_load(path.read_text(encoding="utf-8"))
    metric["filters"] = {"exception_skus": list(EXCEPTIONS)}
    path.write_text(yaml.safe_dump(metric), encoding="utf-8")
    write_metric(catalog_root, "actual.yaml", "sales.outbound_actual", "2.0.0")
    digest = write_release(catalog_root, ["sales.finished_goods_exceptions@1.0.0", "sales.outbound_actual@2.0.0"])
    monkeypatch.setattr("app.agent_core.catalog.active_snapshot", lambda _: (catalog_root, digest))
    add_exceptions(facts)
    class Client:
        def query(self, *, sql, purpose):
            cursor = facts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])
            columns = [column[0] for column in cursor.description]
            rows = [dict(row) for row in cursor]
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}}
    core = AgentOrchestrator(SemanticQueryRuntime(query_client=Client(), clock=lambda: datetime(2026, 9, 10, 12)), project_root=candidate)
    result = core.run_if_supported(QUESTION)
    assert result["query_plan"]["metric_id"] == "sales.finished_goods_exceptions"
    assert result["query_plan"]["metric_version"] == "1.0.0"
    assert result["query_plan"]["release_digest"] == digest
    assert result["agent_core"]["scope"]["finished_goods"] is True
    assert result["agent_core"]["scope"]["finished_goods_exceptions"] == list(EXCEPTIONS)
    plain = core.run_if_supported("昨天成品实际销售出库，按SKU汇总数量")
    assert plain["query_plan"]["metric_id"] == "sales.outbound_actual"
    assert plain["agent_core"]["scope"]["finished_goods_exceptions"] == []
