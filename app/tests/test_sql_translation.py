"""Translation expectations are defined without querying business data."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from app.adhoc_sql import AdHocSqlValidator
from app.sql_translation import SqlTranslationError, translate_mysql_sql


MAPPING = {"legacy.headers": "ywzt_ods.headers", "legacy.details": "ywzt_ods.details"}
SCHEMA = {
    "legacy.headers": {"owner": "TEXT", "id": "TEXT", "created": "DATETIME", "status": "INT"},
    "legacy.details": {"owner": "TEXT", "id": "TEXT", "sku": "TEXT", "qty": "DECIMAL(18, 2)"},
}
SQL = """WITH quantities AS (
 SELECT owner, id, sku, SUM(IFNULL(qty, 0)) AS quantity
 FROM legacy.details GROUP BY owner, id, sku
)
SELECT h.owner AS `货主`, q.sku AS `SKU编码`,
 DATE_FORMAT(h.created, '%Y-%m-%d') AS `日期`,
 SUM(IF(h.status = 700, q.quantity, 0)) AS `实际数量`
FROM legacy.headers h JOIN quantities q ON h.owner = q.owner AND h.id = q.id
WHERE h.created >= DATE_SUB(:end, INTERVAL 1 DAY) AND h.created < :end
GROUP BY h.owner, q.sku, DATE_FORMAT(h.created, '%Y-%m-%d')
ORDER BY h.owner, q.sku"""


def translate(sql=SQL, *, bindings=None, mapping=None, schema=None, max_rows=100):
    return translate_mysql_sql(
        sql, bindings=bindings if bindings is not None else {"end": datetime(2026, 9, 10)},
        table_mapping=MAPPING if mapping is None else mapping,
        source_columns=SCHEMA if schema is None else schema,
        validator=AdHocSqlValidator(allowed_schemas=("ywzt_ods",), max_rows=max_rows),
    )


def test_conversion_preserves_named_projections_sources_and_boundaries():
    result = translate()
    root = sqlglot.parse_one(result.sql, read="doris")
    assert root.named_selects == ["货主", "SKU编码", "日期", "实际数量"]
    assert result.status == "converted_not_executed"
    assert result.row_cap_added is True
    assert result.source_tables == ("ywzt_ods.details", "ywzt_ods.headers")
    assert result.bound_parameter_names == ("end",)
    assert not root.find(exp.Placeholder) and not root.find(exp.Parameter)
    assert root.args["limit"].expression.this == "101"
    assert len(list(root.find_all(exp.Join))) == 1
    assert len(root.args["group"].expressions) == 3
    assert "2026-09-10 00:00:00" in result.sql
    assert "legacy" not in result.sql
    assert result.sql not in repr(result)


def test_translated_query_matches_independent_preaggregated_fixture_reference():
    db = sqlite3.connect(":memory:")
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.execute("CREATE TABLE ywzt_ods.headers(owner TEXT,id TEXT,created TEXT,status INTEGER)")
    db.execute("CREATE TABLE ywzt_ods.details(owner TEXT,id TEXT,sku TEXT,qty INTEGER)")
    headers = [("A", "1", "2026-09-09 00:00:00", 700), ("B", "1", "2026-09-09 23:59:59", 700),
               ("A", "2", "2026-09-10 00:00:00", 700), ("A", "3", "2026-09-08 23:59:59", 700),
               ("A", "4", "2026-09-09 12:00:00", 900)]
    details = [("A", "1", "S1", 2), ("A", "1", "S1", 3), ("A", "1", "S2", None),
               ("B", "1", "S1", 7), ("A", "2", "S1", 99), ("A", "3", "S1", 88), ("A", "4", "S1", 50)]
    db.executemany("INSERT INTO ywzt_ods.headers VALUES (?,?,?,?)", headers)
    db.executemany("INSERT INTO ywzt_ods.details VALUES (?,?,?,?)", details)
    expected = {}
    for owner, document, created, status in headers:
        if "2026-09-09" <= created < "2026-09-10":
            for detail_owner, detail_document, sku, quantity in details:
                if (owner, document) == (detail_owner, detail_document):
                    key = (owner, sku, created[:10])
                    expected[key] = expected.get(key, 0) + ((quantity or 0) if status == 700 else 0)
    try:
        # SQLite lacks DATE_SUB; a fixture-only UDF retains its time component.
        def date_sub(value, amount, unit):
            assert unit == "DAY"
            return (datetime.fromisoformat(value) - timedelta(days=int(amount))).isoformat(sep=" ")
        db.create_function("DATE_SUB", 3, date_sub)
        tree = sqlglot.parse_one(translate().sql, read="doris")
        for node in tree.find_all(exp.DateSub):
            node.set("unit", exp.Literal.string(node.args["unit"].name))
        rows = db.execute(tree.sql(dialect="sqlite")).fetchall()
        assert {tuple(row[:3]): row[3] for row in rows} == expected
    finally:
        db.close()


@pytest.mark.parametrize("value", ["x'; DROP TABLE anything; --", "@x :missing %(other)s", "\\'quoted", "${literal}", "中文"])
def test_parameter_values_remain_literals_not_query_structure(value):
    result = translate("SELECT :value AS label", bindings={"value": value})
    statements = sqlglot.parse(result.sql, read="doris")
    assert len(statements) == 1
    assert statements[0].expressions[0].this.this == value
    assert not list(statements[0].find_all(exp.Table))


@pytest.mark.parametrize("value", [None, True, False, 0, -3, Decimal("123456789.123456789"), date(2026, 9, 9), datetime(2026, 9, 9, 13, 2, 3)])
def test_explicit_scalar_bindings(value):
    result = translate("SELECT :value AS value", bindings={"value": value})
    assert result.bound_parameter_names == ("value",)
    assert ":value" not in result.sql


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.25, Decimal("NaN"), [1], {"x": 1}, datetime(2026, 9, 9, tzinfo=timezone.utc)])
def test_nonexact_or_ambiguous_parameter_types_are_not_guessed(value):
    with pytest.raises(SqlTranslationError):
        translate("SELECT :value", bindings={"value": value})


@pytest.mark.parametrize("sql,bindings", [
    ("SELECT :missing", {}), ("SELECT 1", {"extra": 1}), ("SELECT ?", {}),
    ("SELECT @value", {"value": 1}), ("SELECT %(value)s", {"value": 1}),
    ("SELECT * FROM legacy.headers WHERE ${where}", {}),
    ("SELECT :column FROM :table", {"column": "id", "table": "legacy.headers"}),
])
def test_unbound_unsupported_or_identifier_parameters_fail(sql, bindings):
    with pytest.raises(SqlTranslationError):
        translate(sql, bindings=bindings)


def test_parameter_like_strings_are_not_replaced():
    result = translate("SELECT ':missing @value ${where} %(x)s' AS literal", bindings={})
    assert sqlglot.parse_one(result.sql, read="doris").expressions[0].this.this == ":missing @value ${where} %(x)s"


def test_wildcard_cannot_silently_drop_columns_missing_from_partial_contract():
    with pytest.raises(SqlTranslationError, match="wildcard_requires_complete_schema"):
        translate("SELECT h.* FROM legacy.headers h", bindings={})
    result = translate("SELECT COUNT(*) AS total FROM legacy.headers h", bindings={})
    assert "COUNT(*)" in result.sql


def test_extreme_decimal_binding_rejected_before_expansion():
    with pytest.raises(SqlTranslationError, match="parameter_too_large"):
        translate("SELECT :amount", bindings={"amount": Decimal("1E+1000000000")})


@pytest.mark.parametrize("sql", [
    "UPDATE legacy.headers SET status=1", "SELECT 1; SELECT 2", "SELECT * FROM legacy.headers FOR UPDATE",
    "SELECT SLEEP(1)", "SELECT HTTP_GET('https://example.invalid')", "SELECT @x := 1",
    "SELECT @@global.version", "SELECT * FROM FILES('file'='x')", "SELECT 1 INTO OUTFILE 'x'",
    "WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT 1 FROM x) SELECT * FROM x",
    "SELECT /*! SQL_NO_CACHE */ 1", "SELECT /*+ SET_VAR(query_timeout=9999) */ 1",
])
def test_readonly_backend_policy_cannot_be_bypassed_by_conversion(sql):
    with pytest.raises(SqlTranslationError):
        translate(sql, bindings={})


def test_explicit_limit_preserved_and_excessive_limit_not_silently_rewritten():
    result = translate("SELECT h.id FROM legacy.headers h LIMIT :count OFFSET 3", bindings={"count": 5})
    root = sqlglot.parse_one(result.sql, read="doris")
    assert root.args["limit"].expression.this == "5" and root.args["offset"].expression.this == "3"
    assert result.row_cap_added is False
    with pytest.raises(SqlTranslationError):
        translate("SELECT 1 LIMIT 999", bindings={})


@pytest.mark.parametrize("mapping", [{}, {"legacy.headers": "secret.headers"}, {"legacy.headers": "external.ywzt_ods.headers"}, {"legacy.headers": "headers"}, {"legacy.headers": "ywzt_ods.headers JOIN secret.x"}])
def test_mapping_is_explicit_fully_qualified_and_allowlisted(mapping):
    with pytest.raises(SqlTranslationError):
        translate("SELECT h.id FROM legacy.headers h", bindings={}, mapping=mapping)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM hidden WHERE EXISTS (WITH hidden AS (SELECT id FROM legacy.headers) SELECT 1 FROM hidden)",
    "SELECT * FROM legacy.unknown", "SELECT * FROM headers", "SELECT unknown FROM legacy.headers",
    "SELECT h.unknown FROM legacy.headers h", "SELECT id FROM legacy.headers h JOIN legacy.details d ON h.id=d.id",
])
def test_scope_and_schema_fail_closed(sql):
    with pytest.raises(SqlTranslationError):
        translate(sql, bindings={})


def test_qualified_original_columns_and_nested_cte_shadowing_keep_bindings():
    result = translate("SELECT legacy.headers.id FROM legacy.headers WHERE EXISTS "
                       "(WITH headers AS (SELECT id FROM legacy.details) SELECT 1 FROM headers)", bindings={})
    root = sqlglot.parse_one(result.sql, read="doris")
    assert result.source_tables == ("ywzt_ods.details", "ywzt_ods.headers")
    assert root.expressions[0].alias == "id"
    assert "legacy" not in result.sql


def test_union_all_and_null_preserving_join_are_not_rewritten():
    sql = "SELECT h.id, d.qty FROM legacy.headers h LEFT JOIN legacy.details d ON h.id=d.id " \
          "UNION ALL SELECT h.id, NULL AS qty FROM legacy.headers h"
    root = sqlglot.parse_one(translate(sql, bindings={}).sql, read="doris")
    assert isinstance(root, exp.Union) and root.args.get("distinct") is False
    assert next(root.find_all(exp.Join)).side == "LEFT"


def test_translation_errors_do_not_echo_sql_or_parameters():
    secret = "private-test-marker"
    with pytest.raises(SqlTranslationError) as error:
        translate("SELECT " + secret + " FROM", bindings={})
    assert secret not in str(error.value)
    assert error.value.code


def test_unsupported_parser_warning_never_logs_input(caplog):
    with pytest.raises(SqlTranslationError):
        translate("VACUUM private-test-marker", bindings={})
    assert "private-test-marker" not in caplog.text


def test_database_table_mapping_is_case_exact_not_an_assumed_mysql_server_setting():
    with pytest.raises(SqlTranslationError, match="missing_source_mapping_or_schema"):
        translate("SELECT id FROM LEGACY.HEADERS", bindings={})
    result = translate("SELECT id FROM LEGACY.HEADERS", bindings={},
                       mapping={"LEGACY.HEADERS": "ywzt_ods.headers"},
                       schema={"LEGACY.HEADERS": SCHEMA["legacy.headers"]})
    assert result.source_tables == ("ywzt_ods.headers",)


def test_live_scalar_verifier_is_fixed_before_execution_and_detects_corruption():
    import runpy
    from pathlib import Path
    verifier = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tests/acceptance/check_sql_translation_live.py"))
    translated, expected = verifier["scalar_fixture"]()
    assert translated.source_tables == ()
    assert all(verifier["compare"]([expected], expected).values())
    assert not all(verifier["compare"]([], expected).values())
    for key, value in expected.items():
        mutated = {**expected, key: value + "changed" if isinstance(value, str) else value + 1}
        assert not all(verifier["compare"]([mutated], expected).values())
