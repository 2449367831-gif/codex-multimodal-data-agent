from io import StringIO
import json
from types import SimpleNamespace

import pytest

from app.adhoc_sql import AdHocSqlValidator
from app.sql_translation import SqlTranslationError
from tools.translate_mysql_sql import convert_request, main


SOURCES = [{"id": "ywzt_ods.headers", "fields": {"id": {"type": "string"}, "quantity": {"type": "decimal"}}}]
REQUEST = {"sql": "SELECT id, IFNULL(quantity, :fallback) AS amount FROM legacy.headers",
           "bindings": {"fallback": 0}, "table_mapping": {"legacy.headers": "ywzt_ods.headers"}}


def test_tool_uses_contract_columns_without_guessing_renames():
    validator = AdHocSqlValidator(allowed_schemas=("ywzt_ods",))
    result = convert_request(REQUEST, sources=SOURCES, validator=validator)
    assert result.status == "converted_not_executed"
    for sql in ("SELECT unknown FROM legacy.headers", "SELECT quantity AS amount FROM legacy.other"):
        with pytest.raises(SqlTranslationError):
            convert_request({**REQUEST, "sql": sql, "bindings": {}}, sources=SOURCES, validator=validator)


def test_source_contract_reader_returns_detached_selected_versions():
    from pathlib import Path
    from app.knowledge import KnowledgeBase
    knowledge = KnowledgeBase(Path(__file__).resolve().parents[2], release_id="candidate-0.1.0")
    sources = knowledge.sources()
    assert sources and len({source["id"] for source in sources}) == len(sources)
    original = sources[0]["fields"].copy()
    sources[0]["fields"].clear()
    assert knowledge.sources()[0]["fields"] == original


def test_allowed_schema_does_not_authorize_undeclared_target_table():
    with pytest.raises(SqlTranslationError, match="target_not_in_activated_contracts"):
        convert_request({**REQUEST, "table_mapping": {"legacy.headers": "ywzt_ods.unknown"}},
                        sources=SOURCES, validator=AdHocSqlValidator(allowed_schemas=("ywzt_ods",)))


def test_internal_catalog_mapping_still_uses_same_contract():
    result = convert_request({**REQUEST, "table_mapping": {"legacy.headers": "internal.ywzt_ods.headers"}},
                             sources=SOURCES, validator=AdHocSqlValidator(allowed_schemas=("ywzt_ods",)))
    assert result.source_tables == ("internal.ywzt_ods.headers",)


def test_cli_refuses_candidate_without_active_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.translate_mysql_sql.active_snapshot", lambda root: (tmp_path, None))
    output = StringIO()
    assert main(stdin=StringIO(json.dumps(REQUEST)), stdout=output,
                settings=SimpleNamespace(project_root=tmp_path)) == 1
    assert json.loads(output.getvalue()) == {"status": "translation_rejected", "code": "activated_knowledge_required"}


def test_cli_consumes_trusted_contracts_and_preserves_exact_json_decimal(tmp_path, monkeypatch):
    from decimal import Decimal
    monkeypatch.setattr("tools.translate_mysql_sql.active_snapshot", lambda root: (tmp_path, "trusted-digest"))
    def knowledge(root, **kwargs):
        assert root == tmp_path and kwargs == {
            "reload_on_change": False, "require_trusted_release": True, "trusted_release_sha256": "trusted-digest"}
        return SimpleNamespace(sources=lambda: SOURCES)
    monkeypatch.setattr("tools.translate_mysql_sql.KnowledgeBase", knowledge)
    settings = SimpleNamespace(project_root=tmp_path, sql_allowed_schemas=("ywzt_ods",),
                               sql_allowed_catalogs=("internal",), max_rows=5, max_sql_length=10000)
    request = json.dumps(REQUEST).replace('"fallback": 0', '"fallback": 0.1234567890123456789')
    output = StringIO()
    assert main(stdin=StringIO(request), stdout=output, settings=settings) == 0
    result = json.loads(output.getvalue())
    assert result["status"] == "converted_not_executed" and result["row_cap_added"] is True
    assert str(Decimal("0.1234567890123456789")) in result["sql"]
    assert "LIMIT 6" in result["sql"]
    assert "rows" not in result and "execution" not in result


@pytest.mark.parametrize("raw", ['{"sql":"a","sql":"secret"}', '{"v":NaN}', '{"v":Infinity}', 'secret-invalid-json'])
def test_cli_errors_never_echo_supplied_content(raw):
    output = StringIO()
    assert main(stdin=StringIO(raw), stdout=output) == 1
    assert "secret" not in output.getvalue()
    assert set(json.loads(output.getvalue())) == {"status", "code"}
