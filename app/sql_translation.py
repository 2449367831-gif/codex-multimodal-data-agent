"""Internal MySQL-to-Doris translation; no execution or business-result claims."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, ParseError, UnsupportedError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

from .adhoc_sql import AdHocSqlValidator
from .errors import RuntimeErrorBase


class SqlTranslationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"SQL translation rejected: {code}")


@dataclass(frozen=True)
class TranslatedSql:
    sql: str = field(repr=False)
    source_tables: tuple[str, ...]
    bound_parameter_names: tuple[str, ...]
    row_cap_added: bool
    status: str = "converted_not_executed"


def _relation(value: str, *, source: bool) -> exp.Table:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SqlTranslationError("invalid_table_mapping")
    try:
        statements = sqlglot.parse("SELECT * FROM " + value, read="mysql" if source else "doris",
                                  error_message_context=0)
    except ParseError:
        raise SqlTranslationError("invalid_table_mapping") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise SqlTranslationError("invalid_table_mapping")
    root = statements[0]
    relation = root.args.get("from_")
    table = relation.this if isinstance(relation, exp.From) else None
    if (
        any(value for name, value in root.args.items() if name not in {"expressions", "from_"})
        or not isinstance(table, exp.Table)
        or any(value for name, value in table.args.items() if name not in {"this", "db", "catalog"})
        or not isinstance(table.this, exp.Identifier)
        or not table.db
        or (source and table.catalog)
        or any(node.comments for node in root.walk())
    ):
        raise SqlTranslationError("invalid_table_mapping")
    return table


def _key(table: exp.Table) -> tuple[str, ...]:
    return tuple(part for part in (table.catalog, table.db, table.name) if part)


def _literal(value: Any, *, max_length: int) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int):
        return exp.Literal.number(str(value))
    if isinstance(value, Decimal) and value.is_finite():
        if abs(value.as_tuple().exponent) + len(value.as_tuple().digits) > max_length:
            raise SqlTranslationError("parameter_too_large")
        return exp.Literal.number(format(value, "f"))
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise SqlTranslationError("parameter_timezone_requires_explicit_conversion")
        return exp.Literal.string(value.isoformat(sep=" "))
    if isinstance(value, date):
        return exp.Literal.string(value.isoformat())
    if isinstance(value, str):
        if len(value) > max_length:
            raise SqlTranslationError("parameter_too_large")
        return exp.Literal.string(value)
    raise SqlTranslationError("unsupported_parameter_type")


def _bind(root: exp.Expression, bindings: Mapping[str, Any], *, max_length: int) -> tuple[str, ...]:
    if not isinstance(bindings, Mapping) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        for name in bindings
    ):
        raise SqlTranslationError("invalid_parameter_names")
    placeholders = list(root.find_all(exp.Placeholder))
    names = {node.name for node in placeholders}
    if "" in names or names != set(bindings):
        raise SqlTranslationError("missing_unused_or_positional_parameters")
    for node in placeholders:
        if node.find_ancestor(exp.Table) is not None:
            raise SqlTranslationError("dynamic_table_identifier")
        node.replace(_literal(bindings[node.name], max_length=max_length))
    return tuple(sorted(names))


def _physical_tables(root: exp.Expression) -> list[exp.Table]:
    tables = {}
    for scope in traverse_scope(root):
        # selected_sources detects duplicate aliases, and respects lexical CTE scope.
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                if not isinstance(source.this, exp.Identifier) or not source.db or source.catalog:
                    raise SqlTranslationError("unqualified_or_unsupported_source")
                tables[id(source)] = source
    return list(tables.values())


def translate_mysql_sql(
    sql: str,
    *,
    bindings: Mapping[str, Any],
    table_mapping: Mapping[str, str],
    source_columns: Mapping[str, Mapping[str, str]],
    validator: AdHocSqlValidator,
) -> TranslatedSql:
    """Translate only explicit mappings with known unchanged column names/types.

    The caller supplies schema evidence and backend policy. No optimization of
    facts, joins, predicates or aggregation is performed. The backend's outer
    probe limit is reported separately from the original business expressions.
    """
    if not isinstance(sql, str) or not sql.strip() or len(sql) > validator.max_sql_length:
        raise SqlTranslationError("invalid_sql_length")
    try:
        statements = sqlglot.parse(sql, read="mysql", error_message_context=0)
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise SqlTranslationError("single_select_required")
        root = statements[0]
        if root.find(exp.Select) is None:
            raise SqlTranslationError("single_select_required")
        if root.find(exp.Hint) or any(
            comment.lstrip().startswith(("!", "+"))
            for node in root.walk() for comment in (node.comments or ())
        ):
            raise SqlTranslationError("executable_comments_or_hints")
        if any(root.find(kind) for kind in (exp.Parameter, exp.SessionParameter, exp.PropertyEQ)):
            raise SqlTranslationError("session_parameters_not_supported")
        if any(not isinstance(star.parent, exp.Count) for star in root.find_all(exp.Star)):
            raise SqlTranslationError("wildcard_requires_complete_schema")
        bound_names = _bind(root, bindings, max_length=validator.max_sql_length)
        if not isinstance(table_mapping, Mapping) or not isinstance(source_columns, Mapping):
            raise SqlTranslationError("invalid_table_mapping")
        mappings = {}
        for original, destination in table_mapping.items():
            source = _relation(original, source=True)
            key = _key(source)
            if key in mappings:
                raise SqlTranslationError("duplicate_table_mapping")
            mappings[key] = _relation(destination, source=False)
        schemas = {}
        for original, columns in source_columns.items():
            key = _key(_relation(original, source=True))
            if key in schemas or not isinstance(columns, Mapping) or not columns:
                raise SqlTranslationError("invalid_source_schema")
            if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in columns.items()):
                raise SqlTranslationError("invalid_source_schema")
            schemas[key] = dict(columns)
        schema = MappingSchema(dialect="mysql")
        for table in _physical_tables(root):
            key = _key(table)
            if key not in mappings or key not in schemas:
                raise SqlTranslationError("missing_source_mapping_or_schema")
            schema.add_table(table, schemas[key], dialect="mysql")

        # Resolve fields while the original relation names still exist; aliases
        # then remain stable when the physical database/table identifiers change.
        root = qualify(root, dialect="mysql", schema=schema, infer_schema=False,
                       expand_stars=True, quote_identifiers=True, identify=True)
        for table in _physical_tables(root):
            target = mappings[_key(table)]
            for part in ("this", "db", "catalog"):
                value = target.args.get(part)
                table.set(part, value.copy() if value is not None else None)
        for node in root.walk():
            node.pop_comments()
        converted_sql = root.sql(dialect="doris", unsupported_level=ErrorLevel.RAISE)
        if len(converted_sql) > validator.max_sql_length:
            raise SqlTranslationError("invalid_sql_length")
        reparsed = sqlglot.parse_one(converted_sql, read="doris", error_message_context=0)
        if reparsed.find(exp.Placeholder) or reparsed.find(exp.Parameter):
            raise SqlTranslationError("unresolved_parameters")
        row_cap_added = reparsed.args.get("limit") is None
        validated = validator.validate(converted_sql)
        return TranslatedSql(validated.sql, validated.source_tables, bound_names, row_cap_added)
    except SqlTranslationError:
        raise
    except (ParseError, OptimizeError, UnsupportedError, RuntimeErrorBase, ValueError, TypeError, KeyError):
        # Parser/backend exceptions can include the supplied SQL. Do not expose it.
        raise SqlTranslationError("invalid_or_unsupported_query") from None
