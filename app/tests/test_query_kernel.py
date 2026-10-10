from datetime import datetime

import pytest

from app.query_kernel import QueryPlan, QueryRequest, QueryOutcome


def test_query_plan_accepts_select_and_with_select() -> None:
    QueryPlan("r1", "SELECT 1", "probe").validate_private()
    QueryPlan("r2", "WITH x AS (SELECT 1) SELECT * FROM x", "aggregate").validate_private()


@pytest.mark.parametrize("sql", ["", "UPDATE ywzt_ods.t SET x=1", "SELECT 1; SELECT 2"])
def test_query_plan_rejects_non_single_read_query(sql: str) -> None:
    with pytest.raises(ValueError):
        QueryPlan("r", sql, "query").validate_private()


@pytest.mark.parametrize("sql", [
    "-- business query\nSELECT 1",
    "/* group; aggregate */ WITH x AS (SELECT 1 AS n) SELECT n FROM x",
    "SELECT 'A;B' AS label;",
    "SELECT 'SELECT; DELETE' AS label",
    "SELECT 1 UNION ALL SELECT 2",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "WITH x AS (SELECT ';' AS n) SELECT * FROM x;",
    "SELECT 1; -- trailing comment",
    "/* leading */ SELECT 'A;B' AS label; /* trailing */",
])
def test_query_plan_parses_structure_instead_of_sql_characters(sql):
    from app.adhoc_sql import AdHocSqlValidator
    plan = QueryPlan("r", sql, "query")
    plan.validate_private()
    assert plan.sql == sql
    AdHocSqlValidator(allowed_schemas=("ywzt_ods",)).validate(sql)


@pytest.mark.parametrize("sql", [
    "/* no executable query */", "SELECT (", "SELECT 1; DELETE FROM ywzt_ods.t",
    "WITH changed AS (DELETE FROM ywzt_ods.t RETURNING *) SELECT * FROM changed",
    "SELECT * INTO backup FROM ywzt_ods.t", "SELECT * FROM ywzt_ods.t FOR UPDATE",
])
def test_query_plan_rejects_invalid_and_nested_mutating_statements(sql):
    with pytest.raises(ValueError):
        QueryPlan("r", sql, "query").validate_private()


@pytest.mark.parametrize("sql,purpose", [(None, "q"), (True, "q"), ("SELECT 1", None),
                                        ("SELECT 1", " "), ("SELECT '" + "x" * 100000 + "'", "q")],
                         ids=["missing_sql", "boolean_sql", "missing_purpose", "blank_purpose", "over_limit"])
def test_query_plan_keeps_type_and_size_limits(sql, purpose):
    with pytest.raises(ValueError):
        QueryPlan("r", sql, purpose).validate_private()


def test_query_request_uses_half_open_window() -> None:
    start = datetime(2026, 9, 1)
    end = datetime(2026, 9, 2)
    request = QueryRequest("昨天销量", "r", start_inclusive=start, end_exclusive=end)
    assert request.window == (start, end)
    assert QueryOutcome.COMPLETED.value == "completed"


def test_query_request_rejects_reversed_window() -> None:
    with pytest.raises(ValueError):
        QueryRequest("销量", "r", start_inclusive=datetime(2026, 9, 2), end_exclusive=datetime(2026, 9, 1))
