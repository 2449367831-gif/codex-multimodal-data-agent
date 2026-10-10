"""Conversion intake and screened status; never a database execution route."""

from decimal import Decimal
import json
from pathlib import Path
import re

from .adhoc_sql import AdHocSqlValidator
from .errors import RuntimeErrorBase
from .knowledge import KnowledgeBase
from .knowledge_activation import active_snapshot
from .settings import RuntimeSettings
from .sql_translation import SqlTranslationError, TranslatedSql, translate_mysql_sql, _relation, _key


MAX_CONVERSION_CHARS = 200000
CONVERSION_QUESTION = "将 MySQL 查询转换为 Doris"


def is_conversion_request(question):
    text = str(question or "").casefold()
    return "mysql" in text and "doris" in text and bool(re.search(r"转换|改写|改成|迁移", text))


def convert_request(request, *, sources, validator):
    if not isinstance(request, dict) or set(request) != {"sql", "bindings", "table_mapping"}:
        raise SqlTranslationError("invalid_request_fields")
    mapping = request["table_mapping"]
    if not isinstance(mapping, dict):
        raise SqlTranslationError("invalid_table_mapping")
    contracts = {_key(_relation(source["id"], source=False)): source for source in sources}
    columns = {}
    for original, target in mapping.items():
        target_key = _key(_relation(target, source=False))
        if len(target_key) == 3 and target_key[0] == "internal":
            target_key = target_key[1:]
        contract = contracts.get(target_key)
        if contract is None:
            raise SqlTranslationError("target_not_in_activated_contracts")
        columns[original] = {name: field["type"] for name, field in contract["fields"].items()}
    return translate_mysql_sql(request["sql"], bindings=request["bindings"],
                               table_mapping=mapping, source_columns=columns, validator=validator)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SqlTranslationError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise SqlTranslationError("nonfinite_json_number")


def decode_request(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_CONVERSION_CHARS:
        raise SqlTranslationError("invalid_conversion_input")
    return json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)


def convert_active_request(raw):
    request = decode_request(raw)
    settings = RuntimeSettings.from_env(Path(__file__).resolve().parents[1])
    snapshot, digest = active_snapshot(settings.project_root)
    if digest is None:
        raise SqlTranslationError("activated_knowledge_required")
    knowledge = KnowledgeBase(snapshot, reload_on_change=False, require_trusted_release=True,
                              trusted_release_sha256=digest)
    validator = AdHocSqlValidator(allowed_schemas=settings.sql_allowed_schemas,
                                 allowed_catalogs=settings.sql_allowed_catalogs,
                                 max_rows=settings.max_rows, max_sql_length=settings.max_sql_length)
    return convert_request(request, sources=knowledge.sources(), validator=validator)


def conversion_guidance(raw, *, converter=convert_active_request):
    state = "needs_input"
    text = "尚未转换。缺少待转换的原始查询、明确的库表对应关系及参数值；无参数时也需明确为空。未查询数据。"
    if raw is not None:
        try:
            result = converter(raw)
            if not isinstance(result, TranslatedSql) or result.status != "converted_not_executed":
                raise SqlTranslationError("invalid_conversion_result")
            state = "converted"
            text = "转换检查已完成，未查询数据。已处理日期与条件表达式、参数绑定和明确的库表映射，并校验字段归属与别名。业务口径尚需独立对账，不能据此确认业务结果。"
            if result.row_cap_added:
                text += "已保留后端结果上限；后续执行仍需检查结果是否截断。"
        except (SqlTranslationError, RuntimeErrorBase, ValueError, TypeError, KeyError, OSError, RecursionError):
            state = "rejected"
            text = "转换未通过，未查询数据。请核对材料中的原始查询、库表对应关系、字段和参数；不能缺少必填项、使用未知字段或混入非只读操作。不会自动改写业务口径。"
    return {"format": "plain_text", "kind": "sql_conversion", "title": "查询转换检查", "state": state, "text": text}
