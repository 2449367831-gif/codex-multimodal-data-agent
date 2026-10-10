"""Read-only Doris metadata and bounded quality scanner for the knowledge base.

The source YAML files are the contract.  This module observes Doris and emits a
report of differences; it deliberately never edits a source or metric file.

It can be used as a library (``load_source_tables``, ``scan_doris`` and
``compare_sources``) or as a CLI:

    python tools/metadata_sync.py --mode snapshot \
        --snapshot metadata/examples/doris_snapshot.json
    python tools/metadata_sync.py --mode live --output-dir candidates

Live mode reads connection settings only from ``DORIS_*`` environment
variables.  Every statement issued by the scanner is checked to be a single
SELECT before it reaches the DB-API cursor.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field as dataclass_field
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_DIR = ROOT / "sources" / "tables"
DEFAULT_SNAPSHOT = ROOT / "metadata" / "examples" / "doris_snapshot.json"

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

COLUMNS_SQL = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, "
    "IS_NULLABLE, DATA_TYPE, COLUMN_TYPE, COLUMN_COMMENT "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %s "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
)
TABLES_SQL = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, TABLE_COMMENT, UPDATE_TIME "
    "FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %s "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
)


class MetadataSyncError(RuntimeError):
    """Raised when a source or snapshot cannot be interpreted safely."""


@dataclass(frozen=True)
class SourceTable:
    """A source table contract loaded from ``sources/tables/*.yaml``."""

    id: str
    source_id: str
    database: str
    table: str
    path: str
    role: str | None
    freshness_field: str | None
    freshness_check: str | None
    freshness_config: dict[str, Any]
    fields: dict[str, dict[str, Any]]
    raw: dict[str, Any] = dataclass_field(repr=False, compare=False)
    business_key: str | None = None
    key_quality_enabled: bool = False
    key_quality_lookback_days: int | None = None
    key_quality_config: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass
class ObservedColumn:
    name: str
    data_type: str | None = None
    column_type: str | None = None
    is_nullable: str | None = None
    ordinal_position: int | None = None
    comment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "data_type": self.data_type,
            "column_type": self.column_type,
            "is_nullable": self.is_nullable,
            "ordinal_position": self.ordinal_position,
            "comment": self.comment,
            "canonical_type": canonical_type(self.data_type, self.column_type),
        }


@dataclass
class FreshnessObservation:
    table_id: str
    field: str
    max_value: Any = None
    status: str = "not_checked"
    observed_at: str | None = None
    age_seconds: float | None = None
    error: str | None = None
    query: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class KeyQualityObservation:
    """Aggregate-only business-key quality observation.

    No offending key values or samples are retained. ``duplicate_key_count``
    is the number of excess non-null rows beyond one row per distinct key.
    """

    table_id: str
    field: str
    lookback_days: int
    row_count: int | None = None
    distinct_key_count: int | None = None
    null_key_count: int | None = None
    duplicate_key_count: int | None = None
    status: str = "not_checked"
    observed_at: str | None = None
    error: str | None = None
    query: str | None = None

    def counts_dict(self) -> dict[str, int | None]:
        return {
            "row_count": self.row_count,
            "distinct_key_count": self.distinct_key_count,
            "null_key_count": self.null_key_count,
            "duplicate_key_count": self.duplicate_key_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class ObservedTable:
    schema: str
    name: str
    table_type: str | None = None
    comment: str | None = None
    columns: dict[str, ObservedColumn] = dataclass_field(default_factory=dict)
    freshness: dict[str, FreshnessObservation] = dataclass_field(default_factory=dict)
    key_quality: dict[str, KeyQualityObservation] = dataclass_field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.schema}.{self.name}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "table_schema": self.schema,
            "table_name": self.name,
            "table_type": self.table_type,
            "table_comment": self.comment,
            "columns": [
                self.columns[key].to_dict() for key in sorted(self.columns)
            ],
        }
        if self.freshness:
            result["freshness"] = [
                self.freshness[key].to_dict() for key in sorted(self.freshness)
            ]
        if self.key_quality:
            result["key_quality"] = [
                self.key_quality[key].to_dict() for key in sorted(self.key_quality)
            ]
        return result


@dataclass
class MetadataSnapshot:
    observed_at: str
    tables: dict[str, ObservedTable] = dataclass_field(default_factory=dict)
    collection_status: str = "complete"
    errors: list[str] = dataclass_field(default_factory=list)
    origin: str = "doris"

    @property
    def columns_count(self) -> int:
        return sum(len(table.columns) for table in self.tables.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "observed_at": self.observed_at,
            "collection_status": self.collection_status,
            "origin": self.origin,
            "errors": list(self.errors),
            "tables": [
                self.tables[key].to_dict() for key in sorted(self.tables)
            ],
        }


@dataclass
class CandidateChange:
    kind: str
    table_id: str
    source_file: str
    severity: str
    message: str
    field: str | None = None
    current: Any = None
    observed: Any = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class SyncReport:
    generated_at: str
    mode: str
    source_dir: str
    collection_status: str
    source_table_count: int
    observed_table_count: int
    observed_column_count: int
    candidates: list[CandidateChange] = dataclass_field(default_factory=list)
    freshness: list[FreshnessObservation] = dataclass_field(default_factory=list)
    key_quality: list[KeyQualityObservation] = dataclass_field(default_factory=list)
    undocumented_tables: list[str] = dataclass_field(default_factory=list)
    errors: list[str] = dataclass_field(default_factory=list)
    snapshot_path: str | None = None
    notes: list[str] = dataclass_field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        by_kind = Counter(item.kind for item in self.candidates)
        by_severity = Counter(item.severity for item in self.candidates)
        return _jsonable(
            {
                "schema_version": 1,
                "generated_at": self.generated_at,
                "mode": self.mode,
                "source_dir": self.source_dir,
                "snapshot_path": self.snapshot_path,
                "collection_status": self.collection_status,
                "source_table_count": self.source_table_count,
                "observed_table_count": self.observed_table_count,
                "observed_column_count": self.observed_column_count,
                "summary": {
                    "candidate_count": len(self.candidates),
                    "by_kind": dict(sorted(by_kind.items())),
                    "by_severity": dict(sorted(by_severity.items())),
                    "undocumented_table_count": len(self.undocumented_tables),
                    "freshness_count": len(self.freshness),
                    "key_quality_count": len(self.key_quality),
                },
                "candidates": [item.to_dict() for item in self.candidates],
                "freshness": [item.to_dict() for item in self.freshness],
                "key_quality": [item.to_dict() for item in self.key_quality],
                "undocumented_tables": sorted(self.undocumented_tables),
                "errors": list(self.errors),
                "notes": list(self.notes),
            }
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def doris_now_iso() -> str:
    timezone_name = os.getenv("DORIS_TIMEZONE", "Asia/Shanghai")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise MetadataSyncError(f"unknown DORIS_TIMEZONE: {timezone_name}") from exc
    return datetime.now(zone).isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    """Convert DB-API values into stable JSON values without losing meaning."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _clean_key(key: Any) -> str:
    return str(key).strip().casefold()


def _pick(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    normalized = {_clean_key(key): value for key, value in mapping.items()}
    for name in names:
        if _clean_key(name) in normalized:
            return normalized[_clean_key(name)]
    return default


def _as_mapping(row: Any, description: Sequence[Any] | None = None) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if row is None:
        return {}
    values = list(row) if isinstance(row, (tuple, list)) else [row]
    names: list[str] = []
    for item in description or []:
        if isinstance(item, (tuple, list)):
            names.append(str(item[0]))
        else:
            names.append(str(item))
    if len(names) < len(values):
        names.extend(f"column_{index}" for index in range(len(names), len(values)))
    return dict(zip(names, values))


def _table_key(schema: str, table: str) -> str:
    return f"{schema.casefold()}.{table.casefold()}"


def _parse_table_id(table_id: str) -> tuple[str, str]:
    if not isinstance(table_id, str):
        raise MetadataSyncError("table id must be a string")
    parts = [part.strip().strip("`") for part in table_id.split(".")]
    if len(parts) != 2 or not all(parts):
        raise MetadataSyncError(
            f"table id {table_id!r} must have the form <database>.<table>"
        )
    for part in parts:
        if not IDENTIFIER_RE.fullmatch(part):
            raise MetadataSyncError(f"unsafe identifier in table id {table_id!r}")
    return parts[0], parts[1]


def quote_identifier(identifier: str) -> str:
    """Quote one Doris identifier after enforcing a conservative allow-list."""

    if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier):
        raise MetadataSyncError(f"unsafe Doris identifier: {identifier!r}")
    return f"`{identifier}`"


def build_freshness_query(database: str, table: str, field_name: str) -> str:
    """Build the only data-table query this scanner is allowed to issue."""

    return (
        "SELECT MAX("
        f"{quote_identifier(field_name)}"
        ") AS max_value FROM "
        f"{quote_identifier(database)}.{quote_identifier(table)}"
    )


def build_key_quality_query(
    database: str,
    table: str,
    key_field: str,
    freshness_field: str,
    lookback_days: int,
) -> str:
    """Build one bounded aggregate query without returning business-key values."""

    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
        raise MetadataSyncError("key-quality lookback_days must be an integer")
    if not 1 <= lookback_days <= 3650:
        raise MetadataSyncError("key-quality lookback_days must be between 1 and 3650")
    key_sql = quote_identifier(key_field)
    freshness_sql = quote_identifier(freshness_field)
    table_sql = f"{quote_identifier(database)}.{quote_identifier(table)}"
    null_count = f"COALESCE(SUM(CASE WHEN {key_sql} IS NULL THEN 1 ELSE 0 END), 0)"
    return (
        "SELECT COUNT(*) AS row_count, "
        f"COUNT(DISTINCT {key_sql}) AS distinct_key_count, "
        f"{null_count} AS null_key_count, "
        f"COUNT(*) - COUNT(DISTINCT {key_sql}) - {null_count} AS duplicate_key_count "
        f"FROM {table_sql} "
        f"WHERE {freshness_sql} >= DATE_SUB(NOW(), INTERVAL {lookback_days} DAY)"
    )


def _assert_read_only_select(sql: str) -> None:
    compact = sql.strip()
    if ";" in compact:
        raise MetadataSyncError("metadata scanner refuses multi-statement SQL")
    if not re.match(r"(?is)^select\b", compact):
        raise MetadataSyncError("metadata scanner only permits SELECT statements")


def canonical_type(data_type: Any, column_type: Any = None) -> str | None:
    """Map Doris/MySQL type spellings to the small contract vocabulary."""

    value = str(data_type or column_type or "").strip().casefold()
    if not value:
        return None
    token = re.split(r"[<(\s]", value, maxsplit=1)[0]
    aliases = {
        "char": "string",
        "varchar": "string",
        "string": "string",
        "text": "string",
        "tinytext": "string",
        "mediumtext": "string",
        "longtext": "string",
        "json": "string",
        "variant": "string",
        "hll": "string",
        "bitmap": "string",
        "decimal": "decimal",
        "decimalv2": "decimal",
        "decimalv3": "decimal",
        "numeric": "decimal",
        "tinyint": "integer",
        "smallint": "integer",
        "int": "integer",
        "integer": "integer",
        "bigint": "integer",
        "largeint": "integer",
        "float": "number",
        "double": "number",
        "real": "number",
        "date": "date",
        "datev2": "date",
        "datetime": "datetime",
        "datetimev2": "datetime",
        "timestamp": "datetime",
        "bool": "boolean",
        "boolean": "boolean",
    }
    return aliases.get(token, token)


def types_compatible(expected: Any, observed: Any) -> bool:
    left = canonical_type(expected)
    right = canonical_type(observed)
    if not left or not right:
        return True
    if left == right:
        return True
    # Doris may expose an integral physical type for a quantity contract.
    if left == "decimal" and right in {"integer", "number"}:
        return True
    if left == "number" and right in {"integer", "decimal"}:
        return True
    return False


def _field_spec(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    return {"type": value}


def _is_virtual_field(source: SourceTable, field_name: str) -> bool:
    """Return whether a contract field is supplied by metadata, not the table."""

    for declared_name, spec in source.fields.items():
        if declared_name.casefold() == field_name.casefold():
            return bool(spec.get("virtual"))
    return False


def load_source_tables(sources_dir: str | Path = DEFAULT_SOURCES_DIR) -> list[SourceTable]:
    """Load and normalize all table contracts without touching metric files."""

    path = Path(sources_dir)
    if (path / "tables").is_dir():
        path = path / "tables"
    if not path.is_dir():
        raise MetadataSyncError(f"source table directory does not exist: {path}")

    result: list[SourceTable] = []
    seen: set[str] = set()
    yaml_paths = sorted(
        item
        for pattern in ("*.yaml", "*.yml")
        for item in path.glob(pattern)
        if item.is_file()
    )
    for yaml_path in yaml_paths:
        try:
            with yaml_path.open("r", encoding="utf-8") as stream:
                raw = yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as exc:
            raise MetadataSyncError(f"cannot read {yaml_path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise MetadataSyncError(f"{yaml_path}: top-level YAML must be a mapping")
        table_id = raw.get("id")
        database, table = _parse_table_id(table_id)
        key = _table_key(database, table)
        if key in seen:
            raise MetadataSyncError(f"duplicate source table id: {table_id}")
        seen.add(key)
        fields_raw = raw.get("fields") or {}
        if not isinstance(fields_raw, Mapping):
            raise MetadataSyncError(f"{yaml_path}: fields must be a mapping")
        fields: dict[str, dict[str, Any]] = {}
        for field_name, field_value in fields_raw.items():
            if not isinstance(field_name, str) or not IDENTIFIER_RE.fullmatch(field_name):
                raise MetadataSyncError(
                    f"{yaml_path}: unsafe field identifier {field_name!r}"
                )
            fields[field_name] = _field_spec(field_value)

        freshness_raw = raw.get("freshness") or {}
        if not isinstance(freshness_raw, Mapping):
            raise MetadataSyncError(f"{yaml_path}: freshness must be a mapping")
        freshness_field = freshness_raw.get("field")
        if freshness_field is not None:
            if not isinstance(freshness_field, str) or not IDENTIFIER_RE.fullmatch(
                freshness_field
            ):
                raise MetadataSyncError(
                    f"{yaml_path}: unsafe freshness field {freshness_field!r}"
                )
            if freshness_field not in fields:
                raise MetadataSyncError(
                    f"{yaml_path}: freshness field {freshness_field!r} is not declared"
                )

        business_key = raw.get("business_key")
        if business_key is not None:
            if not isinstance(business_key, str) or not IDENTIFIER_RE.fullmatch(
                business_key
            ):
                raise MetadataSyncError(
                    f"{yaml_path}: unsafe business_key {business_key!r}"
                )
            if business_key not in fields:
                raise MetadataSyncError(
                    f"{yaml_path}: business_key {business_key!r} is not declared"
                )

        metadata_sync_raw = raw.get("metadata_sync") or {}
        if not isinstance(metadata_sync_raw, Mapping):
            raise MetadataSyncError(f"{yaml_path}: metadata_sync must be a mapping")
        key_quality_raw = metadata_sync_raw.get("key_quality") or {}
        if not isinstance(key_quality_raw, Mapping):
            raise MetadataSyncError(
                f"{yaml_path}: metadata_sync.key_quality must be a mapping"
            )
        key_quality_enabled = key_quality_raw.get("enabled", False)
        if not isinstance(key_quality_enabled, bool):
            raise MetadataSyncError(
                f"{yaml_path}: metadata_sync.key_quality.enabled must be boolean"
            )
        lookback_days = key_quality_raw.get("lookback_days")
        if key_quality_enabled:
            if not business_key:
                raise MetadataSyncError(
                    f"{yaml_path}: enabled key_quality requires business_key"
                )
            if not freshness_field:
                raise MetadataSyncError(
                    f"{yaml_path}: enabled key_quality requires freshness.field to bound the scan"
                )
            if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
                raise MetadataSyncError(
                    f"{yaml_path}: enabled key_quality requires integer lookback_days"
                )
            if not 1 <= lookback_days <= 3650:
                raise MetadataSyncError(
                    f"{yaml_path}: key_quality.lookback_days must be between 1 and 3650"
                )
        elif lookback_days is not None and (
            isinstance(lookback_days, bool) or not isinstance(lookback_days, int)
        ):
            raise MetadataSyncError(
                f"{yaml_path}: key_quality.lookback_days must be an integer"
            )
        result.append(
            SourceTable(
                id=str(table_id),
                source_id=str(raw.get("source_id") or ""),
                database=database,
                table=table,
                path=str(yaml_path),
                role=str(raw["role"]) if raw.get("role") is not None else None,
                business_key=business_key,
                freshness_field=freshness_field,
                freshness_check=(
                    str(freshness_raw["check"])
                    if freshness_raw.get("check") is not None
                    else None
                ),
                freshness_config=dict(freshness_raw),
                key_quality_enabled=key_quality_enabled,
                key_quality_lookback_days=(lookback_days if key_quality_enabled else None),
                key_quality_config=dict(key_quality_raw),
                fields=fields,
                raw=dict(raw),
            )
        )
    return result


def _new_observed_table(
    tables: MutableMapping[str, ObservedTable], schema: str, name: str
) -> ObservedTable:
    key = _table_key(schema, name)
    table = tables.get(key)
    if table is None:
        table = ObservedTable(schema=schema, name=name)
        tables[key] = table
    return table


def _column_from_row(row: Mapping[str, Any]) -> tuple[str, str, ObservedColumn] | None:
    schema = _pick(row, "table_schema", "schema", "database")
    table = _pick(row, "table_name", "table")
    name = _pick(row, "column_name", "name")
    if not schema or not table or not name:
        return None
    ordinal = _pick(row, "ordinal_position", "position")
    try:
        ordinal = int(ordinal) if ordinal is not None else None
    except (TypeError, ValueError):
        ordinal = None
    return (
        str(schema),
        str(table),
        ObservedColumn(
            name=str(name),
            data_type=(
                str(_pick(row, "data_type", "type"))
                if _pick(row, "data_type", "type") is not None
                else None
            ),
            column_type=(
                str(_pick(row, "column_type"))
                if _pick(row, "column_type") is not None
                else None
            ),
            is_nullable=(
                str(_pick(row, "is_nullable", "nullable"))
                if _pick(row, "is_nullable", "nullable") is not None
                else None
            ),
            ordinal_position=ordinal,
            comment=(
                str(_pick(row, "column_comment", "comment"))
                if _pick(row, "column_comment", "comment") is not None
                else None
            ),
        ),
    )


def _table_from_row(row: Mapping[str, Any]) -> tuple[str, str, str | None, str | None] | None:
    schema = _pick(row, "table_schema", "schema", "database")
    table = _pick(row, "table_name", "table")
    if not schema or not table:
        return None
    table_type = _pick(row, "table_type", "type")
    comment = _pick(row, "table_comment", "comment")
    return (
        str(schema),
        str(table),
        str(table_type) if table_type is not None else None,
        str(comment) if comment is not None else None,
    )


def _cursor_description(cursor: Any) -> Sequence[Any] | None:
    return getattr(cursor, "description", None)


def _execute_select(cursor: Any, sql: str, params: Sequence[Any] | None = None) -> None:
    _assert_read_only_select(sql)
    if params is None:
        cursor.execute(sql)
    else:
        cursor.execute(sql, params)


def _safe_error(exc: BaseException) -> str:
    # Do not accidentally put a password from an environment variable in a report.
    text = str(exc)
    secret = os.getenv("DORIS_PASSWORD")
    if secret:
        text = text.replace(secret, "[REDACTED_SECRET]")
    return text[:500]


def _nonnegative_count(mapping: Mapping[str, Any], name: str) -> int:
    value = _pick(mapping, name)
    if value is None or isinstance(value, bool):
        raise MetadataSyncError(f"key-quality query did not return {name}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MetadataSyncError(f"key-quality count {name} is invalid") from exc
    if result < 0:
        raise MetadataSyncError(f"key-quality count {name} cannot be negative")
    return result


def scan_doris(
    connection: Any,
    source_tables: Iterable[SourceTable],
    database: str | None = None,
    observed_at: str | None = None,
) -> MetadataSnapshot:
    """Collect source metadata and freshness using a DB-API connection.

    The connection is owned by the caller and is not closed here.  This makes
    the function straightforward to exercise with a fake cursor in tests.
    """

    source_list = list(source_tables)
    schemas = sorted(
        {
            item.database
            for item in source_list
            if database is None or item.database.casefold() == database.casefold()
        }
    )
    snapshot = MetadataSnapshot(
        observed_at=observed_at or doris_now_iso(), origin="doris", collection_status="complete"
    )
    table_update_times: dict[str, Any] = {}
    cursor = connection.cursor()
    try:
        for schema in schemas:
            try:
                _execute_select(cursor, COLUMNS_SQL, (schema,))
                for raw_row in cursor.fetchall() or []:
                    parsed = _column_from_row(
                        _as_mapping(raw_row, _cursor_description(cursor))
                    )
                    if parsed is None:
                        continue
                    row_schema, row_table, column = parsed
                    observed = _new_observed_table(
                        snapshot.tables, row_schema, row_table
                    )
                    observed.columns[column.name.casefold()] = column
            except Exception as exc:  # metadata permissions can differ per schema
                snapshot.errors.append(
                    f"columns scan failed for schema {schema}: {_safe_error(exc)}"
                )
                _rollback_quietly(connection)

            try:
                _execute_select(cursor, TABLES_SQL, (schema,))
                for raw_row in cursor.fetchall() or []:
                    row_map = _as_mapping(raw_row, _cursor_description(cursor))
                    parsed_table = _table_from_row(row_map)
                    if parsed_table is None:
                        continue
                    row_schema, row_table, table_type, comment = parsed_table
                    observed = _new_observed_table(
                        snapshot.tables, row_schema, row_table
                    )
                    observed.table_type = table_type
                    observed.comment = comment
                    table_update_times[_table_key(row_schema, row_table)] = _pick(
                        row_map, "update_time", "table_update_time"
                    )
            except Exception as exc:
                snapshot.errors.append(
                    f"tables scan failed for schema {schema}: {_safe_error(exc)}"
                )
                _rollback_quietly(connection)

        for source in source_list:
            if database is not None and source.database.casefold() != database.casefold():
                continue
            field_name = source.freshness_field
            if not field_name:
                continue
            key = _table_key(source.database, source.table)
            observed = snapshot.tables.get(key)
            if observed is None:
                continue
            query: str | None = None
            try:
                if _is_virtual_field(source, field_name):
                    # The table scan already collected UPDATE_TIME. A virtual
                    # field must never be queried as if it were a physical
                    # column, or the sync report would create false drift.
                    query = TABLES_SQL
                    max_value = table_update_times.get(key)
                else:
                    query = build_freshness_query(
                        source.database, source.table, field_name
                    )
                    _execute_select(cursor, query)
                    row = cursor.fetchone()
                    row_map = _as_mapping(row, _cursor_description(cursor))
                    max_value = _pick(row_map, "max_value", default=None)
                freshness = FreshnessObservation(
                    table_id=source.id,
                    field=field_name,
                    max_value=max_value,
                    status="ok" if max_value is not None else "empty",
                    observed_at=snapshot.observed_at,
                    query=query,
                )
            except Exception as exc:
                _rollback_quietly(connection)
                freshness = FreshnessObservation(
                    table_id=source.id,
                    field=field_name,
                    status="error",
                    observed_at=snapshot.observed_at,
                    error=_safe_error(exc),
                    query=query,
                )
            observed.freshness[field_name.casefold()] = freshness

            if source.key_quality_enabled:
                key_query: str | None = None
                try:
                    if not source.business_key or source.key_quality_lookback_days is None:
                        raise MetadataSyncError(
                            "enabled key-quality observation is missing its bounded configuration"
                        )
                    key_query = build_key_quality_query(
                        source.database,
                        source.table,
                        source.business_key,
                        field_name,
                        source.key_quality_lookback_days,
                    )
                    _execute_select(cursor, key_query)
                    row = cursor.fetchone()
                    row_map = _as_mapping(row, _cursor_description(cursor))
                    row_count = _nonnegative_count(row_map, "row_count")
                    distinct_key_count = _nonnegative_count(
                        row_map, "distinct_key_count"
                    )
                    null_key_count = _nonnegative_count(row_map, "null_key_count")
                    duplicate_key_count = _nonnegative_count(
                        row_map, "duplicate_key_count"
                    )
                    key_quality = KeyQualityObservation(
                        table_id=source.id,
                        field=source.business_key,
                        lookback_days=source.key_quality_lookback_days,
                        row_count=row_count,
                        distinct_key_count=distinct_key_count,
                        null_key_count=null_key_count,
                        duplicate_key_count=duplicate_key_count,
                        status=(
                            "warning"
                            if null_key_count > 0 or duplicate_key_count > 0
                            else "ok"
                        ),
                        observed_at=snapshot.observed_at,
                        query=key_query,
                    )
                except Exception as exc:
                    _rollback_quietly(connection)
                    key_quality = KeyQualityObservation(
                        table_id=source.id,
                        field=source.business_key or "unknown",
                        lookback_days=source.key_quality_lookback_days or 0,
                        status="error",
                        observed_at=snapshot.observed_at,
                        error=_safe_error(exc),
                        query=key_query,
                    )
                observed.key_quality[key_quality.field.casefold()] = key_quality
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
    if snapshot.errors:
        snapshot.collection_status = "partial"
    return snapshot


# A descriptive alias for callers that prefer the longer name.
scan_doris_metadata = scan_doris


def _rollback_quietly(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:
            pass


def _iter_table_entries(value: Any) -> Iterable[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        entries: list[Mapping[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                row = dict(item)
                if not _pick(row, "table_schema", "schema", "database") or not _pick(
                    row, "table_name", "table", "name"
                ):
                    if "." in str(key):
                        schema, table = str(key).split(".", 1)
                        row.setdefault("table_schema", schema)
                        row.setdefault("table_name", table)
                entries.append(row)
        return entries
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _iter_columns(table_entry: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    value = table_entry.get("columns")
    if isinstance(value, Mapping):
        result = []
        for key, item in value.items():
            row = dict(item) if isinstance(item, Mapping) else {"data_type": item}
            row.setdefault("column_name", str(key))
            result.append(row)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _freshness_entries(
    table_entry: Mapping[str, Any], table_id: str, default_observed_at: str | None = None
) -> Iterable[FreshnessObservation]:
    value = table_entry.get("freshness")
    if value is None:
        return []
    rows: list[FreshnessObservation] = []
    if isinstance(value, Mapping) and _pick(value, "field"):
        value = [value]
    elif isinstance(value, Mapping):
        value = [
            ({"field": key, **item} if isinstance(item, Mapping) else {"field": key, "max_value": item})
            for key, item in value.items()
        ]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            field_name = _pick(item, "field", "column", "name")
            if not field_name:
                continue
            max_value = _pick(item, "max_value", "value", "max")
            status = str(_pick(item, "status", default="ok" if max_value is not None else "empty"))
            rows.append(
                FreshnessObservation(
                    table_id=table_id,
                    field=str(field_name),
                    max_value=max_value,
                    status=status,
                    observed_at=(
                        str(_pick(item, "observed_at", "collected_at"))
                        if _pick(item, "observed_at", "collected_at") is not None
                        else default_observed_at
                    ),
                    age_seconds=(
                        float(_pick(item, "age_seconds"))
                        if _pick(item, "age_seconds") is not None
                        else None
                    ),
                    error=(
                        str(_pick(item, "error"))
                        if _pick(item, "error") is not None
                        else None
                    ),
                    query=(
                        str(_pick(item, "query"))
                        if _pick(item, "query") is not None
                        else None
                    ),
                )
            )
    return rows


def _snapshot_count(item: Mapping[str, Any], *names: str) -> int | None:
    value = _pick(item, *names)
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _key_quality_entries(
    table_entry: Mapping[str, Any],
    table_id: str,
    default_observed_at: str | None = None,
) -> Iterable[KeyQualityObservation]:
    """Load aggregate key-quality counts while discarding any sample key values."""

    value = table_entry.get("key_quality")
    if value is None:
        return []
    rows: list[KeyQualityObservation] = []
    if isinstance(value, Mapping) and _pick(value, "field", "business_key", "key_field"):
        value = [value]
    elif isinstance(value, Mapping):
        value = [
            (
                {"field": key, **item}
                if isinstance(item, Mapping)
                else {"field": key}
            )
            for key, item in value.items()
        ]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            field_name = _pick(item, "field", "business_key", "key_field")
            if not field_name:
                continue
            lookback_days = _snapshot_count(item, "lookback_days", "window_days") or 0
            row_count = _snapshot_count(item, "row_count")
            distinct_key_count = _snapshot_count(
                item, "distinct_key_count", "distinct_count"
            )
            null_key_count = _snapshot_count(item, "null_key_count", "null_count")
            duplicate_key_count = _snapshot_count(
                item, "duplicate_key_count", "duplicate_count"
            )
            default_status = "not_checked"
            if all(
                count is not None
                for count in (
                    row_count,
                    distinct_key_count,
                    null_key_count,
                    duplicate_key_count,
                )
            ):
                default_status = (
                    "warning"
                    if (null_key_count or 0) > 0 or (duplicate_key_count or 0) > 0
                    else "ok"
                )
            rows.append(
                KeyQualityObservation(
                    table_id=table_id,
                    field=str(field_name),
                    lookback_days=lookback_days,
                    row_count=row_count,
                    distinct_key_count=distinct_key_count,
                    null_key_count=null_key_count,
                    duplicate_key_count=duplicate_key_count,
                    status=str(_pick(item, "status", default=default_status)),
                    observed_at=(
                        str(_pick(item, "observed_at", "collected_at"))
                        if _pick(item, "observed_at", "collected_at") is not None
                        else default_observed_at
                    ),
                    error=(
                        str(_pick(item, "error"))
                        if _pick(item, "error") is not None
                        else None
                    ),
                    query=(
                        str(_pick(item, "query"))
                        if _pick(item, "query") is not None
                        else None
                    ),
                )
            )
    return rows


def load_snapshot(path: str | Path) -> MetadataSnapshot:
    """Load a normalized JSON snapshot (or a compatible compact fixture)."""

    snapshot_path = Path(path)
    try:
        with snapshot_path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataSyncError(f"cannot read snapshot {snapshot_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise MetadataSyncError("snapshot top-level value must be a mapping")

    observed_at = str(raw.get("observed_at") or raw.get("collected_at") or iso_now())
    status = str(raw.get("collection_status") or raw.get("status") or "complete")
    tables: dict[str, ObservedTable] = {}
    entries = list(_iter_table_entries(raw.get("tables")))
    for table_entry in entries:
        schema = _pick(table_entry, "table_schema", "schema", "database")
        table_name = _pick(table_entry, "table_name", "table", "name")
        if not schema or not table_name:
            continue
        schema, table_name = str(schema), str(table_name)
        observed = _new_observed_table(tables, schema, table_name)
        table_type = _pick(table_entry, "table_type", "type")
        comment = _pick(table_entry, "table_comment", "comment")
        if table_type is not None:
            observed.table_type = str(table_type)
        if comment is not None:
            observed.comment = str(comment)
        for column_entry in _iter_columns(table_entry):
            row = dict(column_entry)
            row.setdefault("table_schema", schema)
            row.setdefault("table_name", table_name)
            parsed = _column_from_row(row)
            if parsed is not None:
                _, _, column = parsed
                observed.columns[column.name.casefold()] = column
        for freshness in _freshness_entries(
            table_entry, observed.id, default_observed_at=observed_at
        ):
            observed.freshness[freshness.field.casefold()] = freshness
        for key_quality in _key_quality_entries(
            table_entry, observed.id, default_observed_at=observed_at
        ):
            observed.key_quality[key_quality.field.casefold()] = key_quality

    # Also accept a flat information_schema-like fixture.
    for raw_row in raw.get("columns", []) if isinstance(raw.get("columns"), Sequence) else []:
        if not isinstance(raw_row, Mapping):
            continue
        parsed = _column_from_row(raw_row)
        if parsed is None:
            continue
        schema, table_name, column = parsed
        observed = _new_observed_table(tables, schema, table_name)
        observed.columns[column.name.casefold()] = column
    for raw_row in raw.get("table_rows", []) if isinstance(raw.get("table_rows"), Sequence) else []:
        if not isinstance(raw_row, Mapping):
            continue
        parsed = _table_from_row(raw_row)
        if parsed is None:
            continue
        schema, table_name, table_type, comment = parsed
        observed = _new_observed_table(tables, schema, table_name)
        observed.table_type, observed.comment = table_type, comment

    top_freshness = raw.get("freshness")
    if isinstance(top_freshness, Mapping):
        for key, item in top_freshness.items():
            if "." not in str(key):
                continue
            schema, table_name = str(key).split(".", 1)
            observed = _new_observed_table(tables, schema, table_name)
            for freshness in _freshness_entries(
                {"freshness": item}, observed.id, default_observed_at=observed_at
            ):
                observed.freshness[freshness.field.casefold()] = freshness
    elif isinstance(top_freshness, Sequence) and not isinstance(top_freshness, (str, bytes)):
        for item in top_freshness:
            if not isinstance(item, Mapping):
                continue
            table_id = _pick(item, "table_id", "id")
            if not table_id or "." not in str(table_id):
                continue
            schema, table_name = str(table_id).split(".", 1)
            observed = _new_observed_table(tables, schema, table_name)
            for freshness in _freshness_entries(
                {"freshness": [item]}, observed.id, default_observed_at=observed_at
            ):
                observed.freshness[freshness.field.casefold()] = freshness

    top_key_quality = raw.get("key_quality")
    if isinstance(top_key_quality, Mapping):
        for key, item in top_key_quality.items():
            if "." not in str(key):
                continue
            schema, table_name = str(key).split(".", 1)
            observed = _new_observed_table(tables, schema, table_name)
            for key_quality in _key_quality_entries(
                {"key_quality": item}, observed.id, default_observed_at=observed_at
            ):
                observed.key_quality[key_quality.field.casefold()] = key_quality
    elif isinstance(top_key_quality, Sequence) and not isinstance(
        top_key_quality, (str, bytes)
    ):
        for item in top_key_quality:
            if not isinstance(item, Mapping):
                continue
            table_id = _pick(item, "table_id", "id")
            if not table_id or "." not in str(table_id):
                continue
            schema, table_name = str(table_id).split(".", 1)
            observed = _new_observed_table(tables, schema, table_name)
            for key_quality in _key_quality_entries(
                {"key_quality": [item]}, observed.id, default_observed_at=observed_at
            ):
                observed.key_quality[key_quality.field.casefold()] = key_quality

    errors = [str(item) for item in (raw.get("errors") or [])]
    return MetadataSnapshot(
        observed_at=observed_at,
        tables=tables,
        collection_status=status,
        errors=errors,
        origin=str(raw.get("origin") or "snapshot"),
    )


def _parse_datetime(
    value: Any, default_timezone: timezone | None = timezone.utc
) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    result = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=default_timezone or timezone.utc)
    return result.astimezone(timezone.utc)


def _freshness_threshold_seconds(config: Mapping[str, Any]) -> float | None:
    for key, multiplier in (
        ("max_age_seconds", 1),
        ("max_age_minutes", 60),
        ("max_age_hours", 3600),
        ("sla_seconds", 1),
        ("sla_minutes", 60),
        ("sla_hours", 3600),
    ):
        value = config.get(key)
        if value is not None:
            try:
                return float(value) * multiplier
            except (TypeError, ValueError):
                return None
    nested = config.get("max_age")
    if isinstance(nested, Mapping):
        for key, multiplier in (("seconds", 1), ("minutes", 60), ("hours", 3600)):
            if nested.get(key) is not None:
                try:
                    return float(nested[key]) * multiplier
                except (TypeError, ValueError):
                    return None
    # Source contracts may express an SLA as Doris-friendly text (for
    # example ``15m``, ``6h`` or ``1d``).  Keep this parser deliberately small
    # so a free-form business note is never silently treated as a duration.
    expected_lag = config.get("expected_lag")
    if isinstance(expected_lag, (int, float)):
        return float(expected_lag)
    if isinstance(expected_lag, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", expected_lag.casefold())
        if match:
            multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            return float(match.group(1)) * multipliers[match.group(2)]
    return None


def _evaluate_freshness(
    observation: FreshnessObservation,
    source: SourceTable,
    now: datetime,
) -> FreshnessObservation:
    if observation.error or observation.status == "error":
        observation.status = "error"
        return observation
    if observation.max_value is None:
        observation.status = "empty"
        return observation
    reference = _parse_datetime(observation.observed_at)
    # A snapshot timestamp commonly carries ``+08:00`` while MAX(datetime)
    # values from Doris are naive strings.  Use the snapshot's original offset
    # for those naive values so an otherwise current table is not marked future.
    default_timezone = timezone.utc
    if isinstance(observation.observed_at, str):
        observed_text = observation.observed_at.strip()
        if observed_text.endswith("Z"):
            observed_text = observed_text[:-1] + "+00:00"
        try:
            observed_raw = datetime.fromisoformat(observed_text)
            if observed_raw.tzinfo is not None:
                default_timezone = observed_raw.tzinfo
        except ValueError:
            pass
    if reference is None:
        default_timezone = timezone.utc
    max_dt = _parse_datetime(observation.max_value, default_timezone)
    if max_dt is None:
        observation.status = "invalid"
        return observation
    age = (now - max_dt).total_seconds()
    observation.age_seconds = age
    if age < 0:
        observation.status = "future"
        return observation
    threshold = _freshness_threshold_seconds(source.freshness_config)
    if threshold is not None and age > threshold:
        observation.status = "stale"
    else:
        observation.status = "ok"
    return observation


def _source_relative_path(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT.resolve()))
    except (ValueError, OSError):
        return path


def _candidate(
    source: SourceTable,
    kind: str,
    severity: str,
    message: str,
    field_name: str | None = None,
    current: Any = None,
    observed: Any = None,
    details: Mapping[str, Any] | None = None,
) -> CandidateChange:
    return CandidateChange(
        kind=kind,
        table_id=source.id,
        source_file=_source_relative_path(source.path),
        severity=severity,
        message=message,
        field=field_name,
        current=current,
        observed=observed,
        details=dict(details or {}),
    )


def compare_sources(
    source_tables: Iterable[SourceTable],
    snapshot: MetadataSnapshot | None,
    mode: str = "snapshot",
    source_dir: str | Path = DEFAULT_SOURCES_DIR,
    snapshot_path: str | Path | None = None,
    now: datetime | None = None,
    include_undocumented_columns: bool = True,
    include_undocumented_tables: bool = True,
) -> SyncReport:
    """Compare contracts with an observation and return candidate changes."""

    source_list = list(source_tables)
    generated_at = iso_now()
    current_time = now or utc_now()
    candidates: list[CandidateChange] = []
    freshness_items: list[FreshnessObservation] = []
    key_quality_items: list[KeyQualityObservation] = []
    undocumented_tables: list[str] = []
    errors: list[str] = []
    notes: list[str] = []
    observed_tables: Mapping[str, ObservedTable] = {}
    collection_status = "not_collected"
    observed_column_count = 0

    if snapshot is not None:
        observed_tables = snapshot.tables
        collection_status = snapshot.collection_status
        errors.extend(snapshot.errors)
        observed_column_count = snapshot.columns_count
        documented = {_table_key(item.database, item.table) for item in source_list}
        if include_undocumented_tables:
            undocumented_tables = [
                table.id
                for key, table in sorted(snapshot.tables.items())
                if key not in documented
            ]
    else:
        notes.append("No snapshot was supplied; no schema drift is inferred.")

    # A partial information_schema result cannot prove that an absent table or
    # field is really gone.  Keep it visible in ``errors`` and freshness output
    # but do not turn incomplete evidence into destructive drift proposals.
    complete_observation = snapshot is not None and snapshot.collection_status == "complete"
    if snapshot is not None and snapshot.collection_status == "partial":
        notes.append("Observation is partial; missing objects are not inferred from it.")
    for source in source_list:
        if snapshot is None or not complete_observation:
            if source.freshness_field:
                freshness_items.append(
                    FreshnessObservation(
                        table_id=source.id,
                        field=source.freshness_field,
                        status="not_checked",
                        observed_at=(snapshot.observed_at if snapshot else None),
                    )
                )
            if source.key_quality_enabled and source.business_key:
                key_quality_items.append(
                    KeyQualityObservation(
                        table_id=source.id,
                        field=source.business_key,
                        lookback_days=source.key_quality_lookback_days or 0,
                        status="not_checked",
                        observed_at=(snapshot.observed_at if snapshot else None),
                    )
                )
            continue

        observed = observed_tables.get(_table_key(source.database, source.table))
        if observed is None:
            candidates.append(
                _candidate(
                    source,
                    "missing_table",
                    "high",
                    "Source table was not returned by Doris information_schema.",
                    observed=False,
                )
            )
            if source.freshness_field:
                freshness_items.append(
                    FreshnessObservation(
                        table_id=source.id,
                        field=source.freshness_field,
                        status="not_checked",
                        observed_at=snapshot.observed_at,
                    )
                )
            if source.key_quality_enabled and source.business_key:
                key_quality_items.append(
                    KeyQualityObservation(
                        table_id=source.id,
                        field=source.business_key,
                        lookback_days=source.key_quality_lookback_days or 0,
                        status="not_checked",
                        observed_at=snapshot.observed_at,
                    )
                )
            continue

        expected_names = {
            name.casefold(): name
            for name, spec in source.fields.items()
            if not bool(spec.get("virtual"))
        }
        for normalized_name, spec in expected_names.items():
            expected = source.fields[spec]
            actual = observed.columns.get(normalized_name)
            if actual is None:
                candidates.append(
                    _candidate(
                        source,
                        "missing_column",
                        "high",
                        f"Expected column {spec} is absent from Doris.",
                        field_name=spec,
                        current=expected.get("type"),
                        observed=None,
                    )
                )
                continue
            expected_type = expected.get("type")
            actual_type = canonical_type(actual.data_type, actual.column_type)
            if expected_type and not types_compatible(expected_type, actual_type):
                candidates.append(
                    _candidate(
                        source,
                        "type_changed",
                        "medium",
                        f"Column {spec} type differs from the source contract.",
                        field_name=spec,
                        current=canonical_type(expected_type),
                        observed=actual_type,
                        details={
                            "observed_data_type": actual.data_type,
                            "observed_column_type": actual.column_type,
                        },
                    )
                )

        for normalized_name, actual in sorted(observed.columns.items()):
            if include_undocumented_columns and normalized_name not in expected_names:
                candidates.append(
                    _candidate(
                        source,
                        "undocumented_column",
                        "low",
                        f"Doris column {actual.name} is not documented in the source contract.",
                        field_name=actual.name,
                        current=None,
                        observed=actual.to_dict(),
                    )
                )

        if source.freshness_field:
            freshness = observed.freshness.get(source.freshness_field.casefold())
            if freshness is None:
                freshness = FreshnessObservation(
                    table_id=source.id,
                    field=source.freshness_field,
                    status="not_checked",
                    observed_at=snapshot.observed_at,
                )
            _evaluate_freshness(freshness, source, current_time)
            freshness_items.append(freshness)
            if freshness.status in {"error", "invalid", "stale", "empty", "future"}:
                severity = "high" if freshness.status == "error" else "medium"
                candidates.append(
                    _candidate(
                        source,
                        "freshness",
                        severity,
                        f"Freshness check for {source.freshness_field} is {freshness.status}.",
                        field_name=source.freshness_field,
                        current=source.freshness_config.get("max_age_hours")
                        or source.freshness_config.get("max_age_minutes"),
                        observed=freshness.to_dict(),
                        details={"status": freshness.status},
                    )
                )

        if source.key_quality_enabled and source.business_key:
            key_quality = observed.key_quality.get(source.business_key.casefold())
            if key_quality is None:
                # Older snapshots do not have key-quality observations. Their
                # absence is compatible and is not evidence of clean or dirty keys.
                key_quality = KeyQualityObservation(
                    table_id=source.id,
                    field=source.business_key,
                    lookback_days=source.key_quality_lookback_days or 0,
                    status="not_checked",
                    observed_at=snapshot.observed_at,
                )
            key_quality_items.append(key_quality)
            duplicate_count = key_quality.duplicate_key_count or 0
            null_count = key_quality.null_key_count or 0
            if duplicate_count > 0 or null_count > 0:
                issue_parts = []
                if duplicate_count > 0:
                    issue_parts.append(f"{duplicate_count} duplicate excess rows")
                if null_count > 0:
                    issue_parts.append(f"{null_count} null-key rows")
                candidates.append(
                    _candidate(
                        source,
                        "business_key_quality",
                        "high" if duplicate_count > 0 else "medium",
                        "Bounded business-key observation found "
                        + " and ".join(issue_parts)
                        + ".",
                        field_name=source.business_key,
                        current={
                            "duplicate_key_count": 0,
                            "null_key_count": 0,
                        },
                        # Aggregate counts only. Never include offending keys.
                        observed=key_quality.counts_dict(),
                        details={
                            "status": key_quality.status,
                            "lookback_days": key_quality.lookback_days,
                        },
                    )
                )

    if undocumented_tables:
        notes.append(
            "Undocumented tables are listed for review; they are not proposed for automatic import."
        )
    if snapshot is not None and not include_undocumented_columns:
        notes.append(
            "Undocumented columns were suppressed because source contracts are runtime field allow-lists; use --include-undocumented-columns for discovery."
        )
    if errors:
        notes.append("One or more metadata queries failed; review errors before accepting candidates.")
    if any(item.status == "error" for item in key_quality_items):
        notes.append(
            "One or more bounded business-key observations failed; no clean-key conclusion is inferred."
        )
    return SyncReport(
        generated_at=generated_at,
        mode=mode,
        source_dir=str(source_dir),
        collection_status=collection_status,
        source_table_count=len(source_list),
        observed_table_count=len(observed_tables),
        observed_column_count=observed_column_count,
        candidates=candidates,
        freshness=freshness_items,
        key_quality=key_quality_items,
        undocumented_tables=undocumented_tables,
        errors=errors,
        snapshot_path=str(snapshot_path) if snapshot_path is not None else None,
        notes=notes,
    )


def render_markdown(report: SyncReport) -> str:
    payload = report.to_dict()
    summary = payload["summary"]
    lines = [
        "# Doris metadata sync report",
        "",
        f"- Generated: `{report.generated_at}`",
        f"- Mode: `{report.mode}`",
        f"- Collection status: `{report.collection_status}`",
        f"- Source tables: `{report.source_table_count}`; observed tables: `{report.observed_table_count}`",
        f"- Candidates: `{summary['candidate_count']}`",
        "",
        "No source or metric YAML file is modified by this report.",
        "",
    ]
    if report.candidates:
        lines.extend(
            [
                "## Candidate changes",
                "",
                "| Kind | Severity | Table | Field | Message |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in report.candidates:
            values = [
                item.kind,
                item.severity,
                item.table_id,
                item.field or "",
                item.message,
            ]
            lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
        lines.append("")
    else:
        lines.extend(["## Candidate changes", "", "No candidate changes detected.", ""])

    lines.extend(["## Freshness", ""])
    if report.freshness:
        lines.extend(
            [
                "| Table | Field | Status | Max value | Age seconds |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in report.freshness:
            values = [
                item.table_id,
                item.field,
                item.status,
                _jsonable(item.max_value),
                item.age_seconds if item.age_seconds is not None else "",
            ]
            lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    else:
        lines.append("No freshness fields are declared in the source contracts.")
    lines.append("")

    lines.extend(["## Business-key quality", ""])
    if report.key_quality:
        lines.extend(
            [
                "| Table | Field | Window days | Status | Rows | Distinct | Null | Duplicate |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in report.key_quality:
            values = [
                item.table_id,
                item.field,
                item.lookback_days,
                item.status,
                item.row_count if item.row_count is not None else "",
                item.distinct_key_count if item.distinct_key_count is not None else "",
                item.null_key_count if item.null_key_count is not None else "",
                item.duplicate_key_count
                if item.duplicate_key_count is not None
                else "",
            ]
            lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    else:
        lines.append("No bounded business-key observations are enabled.")
    lines.append("")

    if report.undocumented_tables:
        lines.extend(["## Undocumented tables", ""])
        lines.extend(f"- `{table_id}`" for table_id in report.undocumented_tables)
        lines.append("")
    if report.errors:
        lines.extend(["## Scan errors", ""])
        lines.extend(f"- {_markdown_cell(error)}" for error in report.errors)
        lines.append("")
    if report.notes:
        lines.extend(["## Notes", ""])
        lines.extend(f"- {_markdown_cell(note)}" for note in report.notes)
        lines.append("")
    return "\n".join(lines)


def _markdown_cell(value: Any) -> str:
    text = str(_jsonable(value) if value is not None else "")
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_report(report: SyncReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Write JSON and Markdown reports under ``output_dir`` only."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    base = target / f"metadata_sync_{stamp}"
    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    # Avoid replacing a same-second report from another process.
    index = 1
    while json_path.exists() or markdown_path.exists():
        json_path = target / f"metadata_sync_{stamp}_{index}.json"
        markdown_path = target / f"metadata_sync_{stamp}_{index}.md"
        index += 1
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _connect_from_env(database: str | None = None) -> Any:
    try:
        import pymysql  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only without optional dep
        raise MetadataSyncError(
            "live mode requires PyMySQL; install requirements.txt before connecting to Doris"
        ) from exc
    host = os.getenv("DORIS_HOST", "127.0.0.1")
    user = os.getenv("DORIS_USER", "")
    password = os.getenv("DORIS_PASSWORD", "")
    db_name = database or os.getenv("DORIS_DATABASE")
    kwargs: dict[str, Any] = {
        "host": host,
        "port": int(os.getenv("DORIS_PORT", "9030")),
        "user": user,
        "password": password,
        "charset": "utf8mb4",
        "connect_timeout": int(os.getenv("DORIS_CONNECT_TIMEOUT", "10")),
        "read_timeout": int(os.getenv("DORIS_QUERY_TIMEOUT", "60")),
        "write_timeout": int(os.getenv("DORIS_QUERY_TIMEOUT", "60")),
        "autocommit": True,
    }
    if db_name:
        kwargs["database"] = db_name
    try:
        return pymysql.connect(**kwargs)
    except Exception as exc:
        raise MetadataSyncError(f"Doris connection failed: {_safe_error(exc)}") from exc


def _resolve_path(value: str | Path, root: Path = ROOT) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("snapshot", "live"),
        default="snapshot",
        help="snapshot reads JSON without a DB connection; live reads Doris (default: snapshot)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="alias for snapshot mode; never opens a Doris connection",
    )
    parser.add_argument(
        "--snapshot",
        help="JSON snapshot path for snapshot mode (defaults to metadata/examples/doris_snapshot.json when present)",
    )
    parser.add_argument(
        "--sources-dir",
        "--source-dir",
        default=str(DEFAULT_SOURCES_DIR),
        dest="sources_dir",
        help="directory containing table contract YAML files",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "candidates"),
        help="directory for generated JSON and Markdown reports",
    )
    parser.add_argument(
        "--database",
        help="limit the scan to one Doris database; live mode otherwise uses DORIS_DATABASE when set",
    )
    parser.add_argument(
        "--format",
        choices=("summary", "json", "markdown"),
        default="summary",
        help="stdout format; reports are always written as JSON and Markdown",
    )
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="return exit code 1 when any candidate change is reported",
    )
    parser.add_argument(
        "--snapshot-out",
        help="optional path to save the normalized live snapshot",
    )
    parser.add_argument(
        "--include-undocumented-columns",
        action="store_true",
        help="report Doris columns outside the runtime field allow-list",
    )
    parser.add_argument(
        "--include-undocumented-tables",
        action="store_true",
        help="list Doris tables that do not yet have source contracts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run and args.mode == "live":
        parser.error("--dry-run cannot be combined with --mode live")
    mode = "snapshot" if args.dry_run else args.mode
    sources_dir = _resolve_path(args.sources_dir)
    try:
        source_tables = load_source_tables(sources_dir)
        snapshot: MetadataSnapshot | None
        snapshot_path: Path | None = None
        connection = None
        if mode == "live":
            # DORIS_DATABASE is both the default connection database and the
            # schema allow-list for the scan.  An explicit CLI value narrows it
            # further without ever exposing a credential on the command line.
            live_database = args.database or os.getenv("DORIS_DATABASE") or None
            connection = _connect_from_env(live_database)
            try:
                snapshot = scan_doris(
                    connection, source_tables, database=live_database
                )
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
            if args.snapshot_out:
                snapshot_out = _resolve_path(args.snapshot_out)
                snapshot_out.parent.mkdir(parents=True, exist_ok=True)
                snapshot_out.write_text(
                    json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        else:
            candidate = _resolve_path(args.snapshot) if args.snapshot else DEFAULT_SNAPSHOT
            if candidate.is_file():
                snapshot_path = candidate
                snapshot = load_snapshot(candidate)
            else:
                snapshot = None
        report = compare_sources(
            source_tables,
            snapshot,
            mode=mode,
            source_dir=sources_dir,
            snapshot_path=snapshot_path,
            include_undocumented_columns=args.include_undocumented_columns,
            include_undocumented_tables=args.include_undocumented_tables,
        )
        json_path, markdown_path = write_report(report, _resolve_path(args.output_dir))
    except MetadataSyncError as exc:
        print(f"metadata sync failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"metadata sync failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(render_markdown(report))
    else:
        summary = report.to_dict()["summary"]
        print(
            "Metadata sync complete: "
            f"{summary['candidate_count']} candidate(s), "
            f"{report.collection_status} observation."
        )
        print(f"JSON report: {json_path}")
        print(f"Markdown report: {markdown_path}")
    return 1 if args.fail_on_drift and report.has_drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
