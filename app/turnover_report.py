"""Typed turnover calculations and a deterministic PNG renderer.

The module is deliberately independent from the chat and query HTTP layers.  A
caller supplies already-aggregated Doris rows (inventory and outbound are in
EA).  The module then applies the requested complete-natural-day denominator,
performs reconciliation checks, and can render a private report image.

No SQL is executed here.  :func:`build_turnover_sql` only returns a parameter-
safe, read-only query template for the Doris executor to validate and run.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .outbound_contract import completed_status_sql, ref_order_type_sql


DEFAULT_TIMEZONE = "Asia/Shanghai"
TURNOVER_WINDOW_DAYS = 7
DEFAULT_TOLERANCE = Decimal("0.01")
NO_MOVEMENT_LABEL = "无动销"
UNMAPPED_CENTER_LABEL = "未映射中心仓"

# This is the reviewed operating-center scope used by the reference-style
# turnover image.  Keep the codes in display order and never accept a caller
# supplied ad-hoc list for this profile.
OPERATING_CENTER_CODES = (
    "WMS000005",  # 成都
    "9813421",  # 合肥
    "9660544",  # 济南
    "TJWWC-777",  # 天津
    "WMS000019",  # 河北
    "10114790",  # 长沙黄兴
    "WMS000232",  # 沈阳
    "WMS000245",  # 郑州
)


class TurnoverReportError(ValueError):
    """Raised when turnover input cannot produce a trustworthy report."""


class TurnoverStatus(str, Enum):
    """Business status for a warehouse turnover row."""

    OK = "ok"
    NO_MOVEMENT = "no_movement"


def _decimal(value: Any, field_name: str) -> Decimal:
    """Convert a Doris numeric value without passing through binary float."""

    if isinstance(value, bool) or value is None:
        raise TurnoverReportError(f"{field_name} must be a finite decimal")
    if isinstance(value, Decimal):
        result = value
    else:
        text = str(value).strip()
        if not text:
            raise TurnoverReportError(f"{field_name} must be a finite decimal")
        try:
            result = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise TurnoverReportError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise TurnoverReportError(f"{field_name} must be a finite decimal")
    return result


def _normalise_text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if not text and not allow_empty:
        raise TurnoverReportError(f"{field_name} is required")
    return text


def _normalise_type(value: Any) -> int | str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        # Doris may return DECIMAL/str for this dimension.  Keep symbolic
        # values usable while normalising numeric values for comparisons.
        numeric = int(text)
    except (TypeError, ValueError):
        return text
    return numeric


def _normalise_node(value: Any) -> int | str | None:
    """Normalise the warehouse hierarchy node without conflating it with type."""

    return _normalise_type(value)


def _parse_datetime(value: Any, *, timezone_name: str = DEFAULT_TIMEZONE) -> datetime:
    """Parse a date/datetime as a timezone-aware local datetime."""

    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise TurnoverReportError(f"unknown timezone: {timezone_name}") from exc

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise TurnoverReportError("window date cannot be empty")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            try:
                parsed = datetime.combine(date.fromisoformat(text), time.min)
            except ValueError:
                raise TurnoverReportError(f"invalid date/datetime: {value!r}") from exc
    else:
        raise TurnoverReportError(f"invalid date/datetime: {value!r}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _window_bounds(
    start: Any,
    end: Any,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    required_days: int | None = TURNOVER_WINDOW_DAYS,
) -> tuple[datetime, datetime, int]:
    if required_days is not None and (not isinstance(required_days, int) or isinstance(required_days, bool) or required_days <= 0):
        raise TurnoverReportError("window days must be a positive integer")
    start_dt = _parse_datetime(start, timezone_name=timezone_name)
    end_dt = _parse_datetime(end, timezone_name=timezone_name)
    if end_dt <= start_dt:
        raise TurnoverReportError("window end must be after window start")
    if start_dt.time() != time.min or end_dt.time() != time.min:
        raise TurnoverReportError("turnover window must use local natural-day midnights")
    delta = end_dt - start_dt
    actual_days = delta.days
    if required_days is not None and actual_days != required_days:
        raise TurnoverReportError(
            f"turnover window must contain exactly {required_days} complete natural days"
        )
    return start_dt, end_dt, actual_days


def _max_timestamp(values: Iterable[Any]) -> Any:
    """Return the latest source timestamp while tolerating missing values."""

    present = [value for value in values if value not in (None, "")]
    if not present:
        return None
    # Source timestamps are retained in their original representation.  For
    # comparison, parse common ISO/date values and fall back to lexical order.
    def key(value: Any) -> tuple[int, Any]:
        try:
            return (1, _parse_datetime(value))
        except TurnoverReportError:
            return (0, str(value))

    return max(present, key=key)


def _is_center_type(value: Any) -> bool:
    if isinstance(value, int):
        return value == 10
    text = str(value or "").strip().lower()
    return text in {"10", "center", "centre", "中心", "中心仓", "仓储/中心仓"}


def _is_detail_type(value: Any) -> bool:
    if isinstance(value, int):
        return value == 11
    text = str(value or "").strip().lower()
    return text in {"11", "detail", "subwarehouse", "分仓", "分仓/仓储"}


def _is_center_row(row: "TurnoverRow") -> bool:
    """Use the hierarchy node first; a node-10 row is its own center.

    ``belonging_center``/``senior_wms`` are operational parent fields and can
    be stale or malformed on a center row. They must not demote a node-10
    warehouse (and its facts) into an unresolved detail row.
    """

    if row.wms_node is not None:
        node = str(row.wms_node).strip()
        return node == "10"
    return _is_center_type(row.wms_type)


def _is_detail_row(row: "TurnoverRow") -> bool:
    if row.wms_node is not None:
        node = str(row.wms_node).strip()
        if node in {"12", "13", "14"}:
            return True
        # Node 10 is always the center layer. A malformed parent on that row
        # is tracked by the SQL quality counter, never reclassified here.
        return False
    return _is_detail_type(row.wms_type)


@dataclass(frozen=True, slots=True)
class TurnoverRow:
    """One warehouse aggregate in EA for the requested outbound window.

    ``outbound_7d_ea`` retains its legacy name and is the *total* valid outbound quantity in the closed-
    open window.  The displayed movement value is derived as
    ``outbound_7d_ea / window_days``; it is intentionally not accepted as a silently
    interchangeable average.
    """

    wms_code: str
    warehouse_name: str
    available_ea: Decimal | int | str | float
    outbound_7d_ea: Decimal | int | str | float
    wms_type: int | str | None = None
    wms_node: int | str | None = None
    belonging_center: str | None = None
    belonging_center_code: str | None = None
    center_order: int | None = None
    source_max_stock_change_time: Any = None
    source_max_ship_at: Any = None
    source_max_inventory_update_time: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "wms_code", _normalise_text(self.wms_code, "wms_code"))
        object.__setattr__(
            self, "warehouse_name", _normalise_text(self.warehouse_name, "warehouse_name")
        )
        center = None
        if self.belonging_center is not None and str(self.belonging_center).strip():
            center = str(self.belonging_center).strip()
        object.__setattr__(self, "belonging_center", center)
        center_code = None
        if self.belonging_center_code is not None and str(self.belonging_center_code).strip():
            center_code = str(self.belonging_center_code).strip()
        object.__setattr__(self, "belonging_center_code", center_code)
        object.__setattr__(self, "wms_type", _normalise_type(self.wms_type))
        object.__setattr__(self, "wms_node", _normalise_node(self.wms_node))
        if self.center_order is None or str(self.center_order).strip() == "":
            normalized_center_order = None
        else:
            try:
                normalized_center_order = int(self.center_order)
            except (TypeError, ValueError) as exc:
                raise TurnoverReportError("center_order must be an integer") from exc
            if normalized_center_order < 0:
                raise TurnoverReportError("center_order cannot be negative")
        object.__setattr__(self, "center_order", normalized_center_order)
        inventory = _decimal(self.available_ea, "available_ea")
        outbound = _decimal(self.outbound_7d_ea, "outbound_7d_ea")
        if inventory < 0:
            raise TurnoverReportError("available_ea cannot be negative")
        if outbound < 0:
            raise TurnoverReportError("outbound_7d_ea cannot be negative")
        object.__setattr__(self, "available_ea", inventory)
        object.__setattr__(self, "outbound_7d_ea", outbound)

    @classmethod
    def from_mapping(
        cls,
        row: Mapping[str, Any],
        *,
        default_center: str | None = None,
    ) -> "TurnoverRow":
        """Create a typed row from a Doris dictionary or a Chinese-labelled row."""

        if not isinstance(row, Mapping):
            raise TurnoverReportError("turnover row must be a mapping")

        def pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in row and row[name] is not None:
                    return row[name]
            return default

        wms_code = pick("wms_code", "warehouse_code", "仓库编码")
        warehouse_name = pick("warehouse_name", "wms_desc", "warehouse", "仓库名称", "分仓")
        available = pick(
            "available_ea",
            "inventory_ea",
            "available_quantity_ea",
            "实时库存量",
            "库存量",
        )
        outbound = pick(
            "outbound_7d_ea",
            "movement_7d_ea",
            "outbound_ea_7d",
            "valid_outbound_7d_ea",
            "近7天出库量",
            "近7天动销",
        )
        center = pick(
            "belonging_center",
            "center_name",
            "center_warehouse",
            "归属中心仓",
            default=default_center,
        )
        center_code = pick(
            "belonging_center_code",
            "center_code",
            "center_warehouse_code",
            "归属中心仓编码",
        )
        if outbound is None and pick("avg_daily_outbound_ea", "daily_outbound_ea") is not None:
            raise TurnoverReportError(
                "daily outbound average is not accepted as outbound_7d_ea; provide the seven-day total"
            )
        return cls(
            wms_code=wms_code,
            warehouse_name=warehouse_name,
            available_ea=available,
            outbound_7d_ea=outbound,
            wms_type=pick("wms_type", "warehouse_type", "仓库类型"),
            wms_node=pick("wms_node", "node", "warehouse_node", "仓库节点"),
            belonging_center=center,
            belonging_center_code=center_code,
            center_order=pick("center_order", "中心仓顺序"),
            source_max_stock_change_time=pick(
                "source_max_stock_change_time", "max_stock_change_time", "库存最大变更时间"
            ),
            source_max_ship_at=pick("source_max_ship_at", "max_ship_at", "出库最大时间"),
            source_max_inventory_update_time=pick(
                "source_max_inventory_update_time", "库存源最大更新时间"
            ),
        )

    def average_daily_outbound(self, window_days: int = TURNOVER_WINDOW_DAYS) -> Decimal:
        if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days <= 0:
            raise TurnoverReportError("window days must be a positive integer")
        return self.outbound_7d_ea / Decimal(window_days)

    @property
    def avg_daily_outbound_ea(self) -> Decimal:
        return self.average_daily_outbound()

    def turnover_days_for(self, window_days: int = TURNOVER_WINDOW_DAYS) -> Decimal | None:
        average = self.average_daily_outbound(window_days)
        if average == 0:
            return None
        return self.available_ea / average

    @property
    def turnover_days(self) -> Decimal | None:
        return self.turnover_days_for()

    def status_for(self, window_days: int = TURNOVER_WINDOW_DAYS) -> TurnoverStatus:
        return TurnoverStatus.NO_MOVEMENT if self.average_daily_outbound(window_days) == 0 else TurnoverStatus.OK

    @property
    def status(self) -> TurnoverStatus:
        return self.status_for()

    def as_dict(self, *, window_days: int = TURNOVER_WINDOW_DAYS) -> dict[str, Any]:
        days = self.turnover_days_for(window_days)
        return {
            "wms_code": self.wms_code,
            "warehouse_name": self.warehouse_name,
            "wms_type": self.wms_type,
            "wms_node": self.wms_node,
            "belonging_center": self.belonging_center,
            "belonging_center_code": self.belonging_center_code,
            "center_order": self.center_order,
            "available_ea": str(self.available_ea),
            "outbound_7d_ea": str(self.outbound_7d_ea),
            "avg_daily_outbound_ea": str(self.average_daily_outbound(window_days)),
            "turnover_days": None if days is None else str(days),
            "status": self.status_for(window_days).value,
            "source_max_stock_change_time": self.source_max_stock_change_time,
            "source_max_ship_at": self.source_max_ship_at,
            "source_max_inventory_update_time": self.source_max_inventory_update_time,
        }


# A descriptive alias used by callers that prefer the domain term.
TurnoverMetricRow = TurnoverRow


def _aggregate_rows(
    rows: Sequence[TurnoverRow],
    *,
    wms_code: str,
    warehouse_name: str,
    belonging_center: str | None,
    belonging_center_code: str | None = None,
    wms_type: int | str | None = None,
    wms_node: int | str | None = None,
    center_order: int | None = None,
) -> TurnoverRow:
    if not rows:
        raise TurnoverReportError("cannot aggregate an empty row collection")
    return TurnoverRow(
        wms_code=wms_code,
        warehouse_name=warehouse_name,
        available_ea=sum((row.available_ea for row in rows), Decimal("0")),
        outbound_7d_ea=sum((row.outbound_7d_ea for row in rows), Decimal("0")),
        wms_type=wms_type,
        wms_node=wms_node,
        belonging_center=belonging_center,
        belonging_center_code=belonging_center_code,
        center_order=center_order,
        source_max_stock_change_time=_max_timestamp(
            row.source_max_stock_change_time for row in rows
        ),
        source_max_ship_at=_max_timestamp(row.source_max_ship_at for row in rows),
        source_max_inventory_update_time=_max_timestamp(
            row.source_max_inventory_update_time for row in rows
        ),
    )


@dataclass(frozen=True, slots=True)
class TurnoverGroup:
    """A center summary and its optional subwarehouse detail rows."""

    center: TurnoverRow
    details: tuple[TurnoverRow, ...] = ()
    summary_derived: bool = False
    center_candidates: tuple[TurnoverRow, ...] = ()
    unmapped: bool = False
    # ``center_key`` is the stable source code used for grouping.  The display
    # name is deliberately kept separate because two centers may share a name
    # (or a name may change) without being the same warehouse.
    center_key: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", tuple(self.details))
        object.__setattr__(self, "center_candidates", tuple(self.center_candidates))
        key = self.center_key
        if key is not None:
            key = str(key).strip() or None
        if key is None and not self.unmapped:
            key = self.center.belonging_center_code or self.center.wms_code
        object.__setattr__(self, "center_key", key)
        display = self.display_name
        if display is not None:
            display = str(display).strip() or None
        if display is None and not self.unmapped:
            display = self.center.belonging_center or self.center.warehouse_name
        object.__setattr__(self, "display_name", display)
        center_name = self.center_name
        expected_code = self.center_code
        for row in self.details:
            if self.unmapped:
                continue
            if row.belonging_center_code and expected_code:
                if row.belonging_center_code != expected_code:
                    raise TurnoverReportError(
                        f"detail row {row.wms_code!r} maps to {row.belonging_center_code!r}, expected {expected_code!r}"
                    )
                continue
            # Legacy fixture/input rows may carry only an explicit parent
            # name.  Name comparison is a compatibility fallback only; SQL
            # rows are required to carry the code path above.
            row_center = row.belonging_center
            if row_center and row_center != center_name:
                raise TurnoverReportError(
                    f"detail row {row.wms_code!r} belongs to {row_center!r}, expected {center_name!r}"
                )

    @property
    def center_name(self) -> str:
        if self.unmapped:
            return UNMAPPED_CENTER_LABEL
        return self.display_name or self.center.belonging_center or self.center.warehouse_name

    @property
    def center_code(self) -> str | None:
        if self.unmapped:
            return None
        return self.center_key or self.center.belonging_center_code or self.center.wms_code

    def as_dict(self, *, window_days: int = TURNOVER_WINDOW_DAYS) -> dict[str, Any]:
        return {
            "center": self.center.as_dict(window_days=window_days),
            "details": [row.as_dict(window_days=window_days) for row in self.details],
            "summary_derived": self.summary_derived,
            "unmapped": self.unmapped,
            "center_code": self.center_code,
            "center_name": self.center_name,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationCheck:
    name: str
    passed: bool
    severity: str = "blocking"
    expected: Decimal | None = None
    actual: Decimal | None = None
    difference: Decimal | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        def serial(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "expected": serial(self.expected),
            "actual": serial(self.actual),
            "difference": serial(self.difference),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    checks: tuple[ReconciliationCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks if check.severity == "blocking")

    @property
    def status(self) -> str:
        if not self.passed:
            return "blocked"
        if any(
            not check.passed and check.severity in {"warning", "information"}
            for check in self.checks
        ):
            return "usable_with_warning"
        return "usable"

    @property
    def failures(self) -> tuple[ReconciliationCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class TurnoverReport:
    """Complete typed report ready for reconciliation and rendering."""

    window_start: datetime
    window_end_exclusive: datetime
    groups: tuple[TurnoverGroup, ...]
    profile_name: str = "default"
    timezone_name: str = DEFAULT_TIMEZONE
    unit: str = "EA"
    tolerance: Decimal = DEFAULT_TOLERANCE
    source_rows: tuple[TurnoverRow, ...] = field(default_factory=tuple, repr=False)
    duplicate_warehouse_codes: tuple[str, ...] = ()
    duplicate_center_codes: tuple[str, ...] = ()
    unknown_type_codes: tuple[str, ...] = ()
    inventory_freshness_mode: str = "row_change_time"
    center_summary_mode: str = "independent"

    def __post_init__(self) -> None:
        object.__setattr__(self, "groups", tuple(self.groups))
        object.__setattr__(self, "source_rows", tuple(self.source_rows))
        object.__setattr__(self, "duplicate_warehouse_codes", tuple(self.duplicate_warehouse_codes))
        object.__setattr__(self, "duplicate_center_codes", tuple(self.duplicate_center_codes))
        object.__setattr__(self, "unknown_type_codes", tuple(self.unknown_type_codes))
        freshness_mode = str(self.inventory_freshness_mode or "").strip().casefold()
        if freshness_mode not in {"row_change_time", "table_update_time"}:
            raise TurnoverReportError("unsupported inventory freshness mode")
        object.__setattr__(self, "inventory_freshness_mode", freshness_mode)
        summary_mode = str(self.center_summary_mode or "").strip().casefold()
        if summary_mode not in {"independent", "rollup_scope"}:
            raise TurnoverReportError("unsupported center summary mode")
        object.__setattr__(self, "center_summary_mode", summary_mode)
        if not str(self.profile_name).strip():
            raise TurnoverReportError("profile_name is required")
        if str(self.unit).upper() != "EA":
            raise TurnoverReportError("turnover report currently requires EA input")
        tolerance = _decimal(self.tolerance, "tolerance")
        if tolerance < 0:
            raise TurnoverReportError("tolerance cannot be negative")
        object.__setattr__(self, "tolerance", tolerance)

    @property
    def window_days(self) -> int:
        return (self.window_end_exclusive - self.window_start).days

    @property
    def center_rows(self) -> tuple[TurnoverRow, ...]:
        return tuple(group.center for group in self.groups)

    @property
    def detail_rows(self) -> tuple[TurnoverRow, ...]:
        return tuple(row for group in self.groups for row in group.details)

    @property
    def center_total(self) -> TurnoverRow | None:
        if not self.center_rows:
            return None
        return _aggregate_rows(
            self.center_rows,
            wms_code="__report_total__",
            warehouse_name="合计",
            belonging_center="合计",
            wms_type=10,
        )

    @property
    def detail_total(self) -> TurnoverRow | None:
        if not self.detail_rows:
            return None
        return _aggregate_rows(
            self.detail_rows,
            wms_code="__detail_total__",
            warehouse_name="明细合计",
            belonging_center="合计",
            wms_type=11,
        )

    @property
    def reconciliation(self) -> ReconciliationResult:
        return validate_reconciliation(self)

    @property
    def quality_status(self) -> str:
        return self.reconciliation.status

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "timezone": self.timezone_name,
            "window_start": self.window_start.isoformat(),
            "window_end_exclusive": self.window_end_exclusive.isoformat(),
            "window_days": self.window_days,
            "unit": self.unit,
            "inventory_freshness_mode": self.inventory_freshness_mode,
            "center_summary_mode": self.center_summary_mode,
            "unknown_type_codes": list(self.unknown_type_codes),
            "centers": [group.as_dict(window_days=self.window_days) for group in self.groups],
            "reconciliation": self.reconciliation.as_dict(),
        }


def _coerce_rows(
    rows: Iterable[TurnoverRow | Mapping[str, Any]],
    *,
    default_center: str | None = None,
) -> tuple[TurnoverRow, ...]:
    result: list[TurnoverRow] = []
    for row in rows:
        result.append(
            row if isinstance(row, TurnoverRow) else TurnoverRow.from_mapping(row, default_center=default_center)
        )
    if not result:
        raise TurnoverReportError("at least one turnover row is required")
    return tuple(result)


def build_turnover_report(
    rows: Iterable[TurnoverRow | Mapping[str, Any]],
    start: Any,
    end: Any,
    *,
    profile_name: str = "default",
    center_rows: Iterable[TurnoverRow | Mapping[str, Any]] | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    tolerance: Decimal | int | str | float = DEFAULT_TOLERANCE,
    inventory_freshness_mode: str = "row_change_time",
    center_summary_mode: str = "independent",
    window_days: int = TURNOVER_WINDOW_DAYS,
) -> TurnoverReport:
    """Build a report from warehouse-level Doris aggregates.

    Rows with ``wms_node=10`` are treated as center summaries and rows with
    ``wms_node`` 12/13/14 as details.  When the node is absent, the legacy
    ``wms_type`` values 10/11 are used as a fallback.  When no center summary
    is supplied for a group, a summary is derived from its details and marked
    ``summary_derived=True``. Node-10 rows are keyed to themselves; a
    ``belonging_center`` mapping is never guessed for node12/13/14 details.
    """

    start_dt, end_dt, _ = _window_bounds(
        start,
        end,
        timezone_name=timezone_name,
        required_days=window_days,
    )
    typed_rows = _coerce_rows(rows)
    # ``center_rows`` is an explicit layer and therefore does not require a
    # wms_type marker.  Flat rows use wms_type=10/11 to identify their layer.
    supplied_centers = _coerce_rows(center_rows) if center_rows is not None else ()
    flat_centers = tuple(row for row in typed_rows if _is_center_row(row))
    flat_details = tuple(row for row in typed_rows if _is_detail_row(row))
    unknown_type_codes = tuple(
        sorted(
            {
                row.wms_code
                for row in typed_rows
                if not _is_center_row(row) and not _is_detail_row(row)
            }
        )
    )
    explicit_centers = flat_centers + supplied_centers
    all_rows = typed_rows + supplied_centers

    centers_by_key: dict[str, list[TurnoverRow]] = defaultdict(list)
    details_by_key: dict[str, list[TurnoverRow]] = defaultdict(list)
    center_display_by_key: dict[str, str] = {}
    center_keys_by_legacy_name: dict[str, set[str]] = defaultdict(set)
    unmapped_keys: set[str] = set()

    def center_key_for_center(row: TurnoverRow) -> str:
        # A center's own WMS code is the primary identity.  A supplied legacy
        # row without a code can still be grouped by its explicit name, but it
        # is never allowed to collide with a coded center.
        code = row.wms_code or row.belonging_center_code
        return f"code:{code}" if code else f"name:{row.warehouse_name}"

    for row in explicit_centers:
        key = center_key_for_center(row)
        centers_by_key[key].append(row)
        display = row.belonging_center or row.warehouse_name
        center_display_by_key.setdefault(key, display)
        center_keys_by_legacy_name[display].add(key)

    for row in flat_details:
        if row.belonging_center_code:
            # SQL-produced rows carry the stable parent code.  This path is
            # deliberately independent of the display name so same-named
            # centers cannot merge.
            key = f"code:{row.belonging_center_code}"
            if key not in centers_by_key:
                # A parent code with no valid center candidate is unresolved;
                # retaining it under a synthetic group makes the quality gate
                # fail instead of silently deriving a false center summary.
                unmapped_keys.add(key)
        elif row.belonging_center:
            # Compatibility for older/manual inputs that provide only an
            # explicit parent name.  It is accepted only when that name maps
            # to exactly one center; duplicate names are ambiguous.
            name = row.belonging_center
            candidates = sorted(center_keys_by_legacy_name.get(name, set()))
            if len(candidates) == 1:
                key = candidates[0]
            elif len(candidates) > 1:
                key = f"{UNMAPPED_CENTER_LABEL}:name:{name}:{row.wms_code}"
                unmapped_keys.add(key)
            else:
                key = f"name:{name}"
                unmapped_keys.add(key)
        else:
            # Keep each unmapped warehouse isolated.  Combining unrelated
            # warehouses under one synthetic center would make totals unsafe.
            key = f"{UNMAPPED_CENTER_LABEL}:code:{row.wms_code}"
            unmapped_keys.add(key)
        details_by_key[key].append(row)

    center_order_by_key: dict[str, int] = {}
    for key, candidates in centers_by_key.items():
        orders = [row.center_order for row in candidates if row.center_order is not None]
        if orders:
            center_order_by_key[key] = min(orders)

    ordered_keys = sorted(
        set(centers_by_key) | set(details_by_key),
        key=lambda key: (center_order_by_key.get(key, 1_000_000), key),
    )
    groups: list[TurnoverGroup] = []
    for key in ordered_keys:
        candidates = tuple(centers_by_key.get(key, ()))
        details = tuple(details_by_key.get(key, ()))
        is_unmapped = key in unmapped_keys
        display_name = center_display_by_key.get(key)
        summary_mode = str(center_summary_mode or "").strip().casefold()
        if summary_mode == "rollup_scope" and candidates:
            center = _aggregate_rows(
                candidates + details,
                wms_code=candidates[0].wms_code,
                warehouse_name=display_name or candidates[0].warehouse_name,
                belonging_center=display_name or candidates[0].belonging_center,
                belonging_center_code=(
                    key.removeprefix("code:")
                    if key.startswith("code:") and not is_unmapped
                    else candidates[0].belonging_center_code
                ),
                wms_type=10,
                wms_node=10,
                center_order=center_order_by_key.get(key),
            )
            derived = bool(details) or len(candidates) > 1
        elif candidates:
            center = (
                candidates[0]
                if len(candidates) == 1
                else _aggregate_rows(
                    candidates,
                    wms_code=f"__center__:{key}",
                    warehouse_name=display_name or key,
                    belonging_center=None if is_unmapped else display_name or key,
                    belonging_center_code=(
                        key.removeprefix("code:")
                        if key.startswith("code:") and not is_unmapped
                        else None
                    ),
                    wms_type=10,
                    wms_node=10,
                    center_order=center_order_by_key.get(key),
                )
            )
            derived = len(candidates) > 1
        elif details:
            # A detail-only input remains useful, but its center line is marked
            # derived so consumers know no independent center statistic exists.
            center = _aggregate_rows(
                details,
                wms_code=f"__center__:{key}",
                warehouse_name=display_name or key,
                belonging_center=None if is_unmapped else key,
                belonging_center_code=(
                    key.removeprefix("code:")
                    if key.startswith("code:") and not is_unmapped
                    else None
                ),
                wms_type=10,
                wms_node=10,
                center_order=center_order_by_key.get(key),
            )
            derived = True
        else:  # pragma: no cover - guarded by key construction
            raise TurnoverReportError(f"center {key!r} has no usable rows")
        groups.append(
            TurnoverGroup(
                center=center,
                details=details,
                summary_derived=derived,
                center_candidates=candidates,
                unmapped=is_unmapped,
                center_key=(
                    key.removeprefix("code:")
                    if key.startswith("code:") and not is_unmapped
                    else None
                ),
                display_name=display_name,
            )
        )

    # Duplicate checks are layer-aware.  The same physical code appearing in
    # both layers is still surfaced as a duplicate warehouse key; no silent
    # de-duplication is performed.
    def duplicate_codes(layer: Iterable[TurnoverRow]) -> tuple[str, ...]:
        counts: dict[str, int] = defaultdict(int)
        for row in layer:
            counts[row.wms_code] += 1
        return tuple(sorted(code for code, count in counts.items() if count > 1))

    duplicate_warehouse_codes = duplicate_codes(all_rows)
    duplicate_center_codes = duplicate_codes(explicit_centers)
    return TurnoverReport(
        window_start=start_dt,
        window_end_exclusive=end_dt,
        groups=tuple(groups),
        profile_name=str(profile_name).strip(),
        timezone_name=timezone_name,
        tolerance=_decimal(tolerance, "tolerance"),
        source_rows=all_rows,
        duplicate_warehouse_codes=duplicate_warehouse_codes,
        duplicate_center_codes=duplicate_center_codes,
        unknown_type_codes=unknown_type_codes,
        inventory_freshness_mode=inventory_freshness_mode,
        center_summary_mode=center_summary_mode,
    )


def _close_enough(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    return abs(left - right) <= tolerance


def _check(
    name: str,
    passed: bool,
    *,
    expected: Decimal | None = None,
    actual: Decimal | None = None,
    tolerance: Decimal | None = None,
    severity: str = "blocking",
    message: str = "",
) -> ReconciliationCheck:
    difference = None if expected is None or actual is None else actual - expected
    return ReconciliationCheck(
        name=name,
        passed=passed,
        severity=severity,
        expected=expected,
        actual=actual,
        difference=difference,
        message=message,
    )


def validate_reconciliation(report: TurnoverReport) -> ReconciliationResult:
    """Run layer-local checks without comparing independent center/detail stats."""

    tolerance = report.tolerance
    checks: list[ReconciliationCheck] = []
    checks.append(
        _check(
            "complete_natural_day_window",
            report.window_days > 0,
            expected=Decimal(report.window_days),
            actual=Decimal(report.window_days),
            message="window must contain complete natural days",
        )
    )
    checks.append(
        _check(
            "unique_warehouse_keys",
            not report.duplicate_warehouse_codes,
            severity="blocking",
            message=(
                "duplicate warehouse codes: " + ", ".join(report.duplicate_warehouse_codes)
                if report.duplicate_warehouse_codes
                else ""
            ),
        )
    )
    checks.append(
        _check(
            "unique_center_keys",
            not report.duplicate_center_codes,
            severity="blocking",
            message=(
                "duplicate center codes: " + ", ".join(report.duplicate_center_codes)
                if report.duplicate_center_codes
                else ""
            ),
        )
    )

    # A resolved center mapping is required for non-zero detail facts.  Zero
    # rows are retained for visibility but do not make the whole report unsafe.
    unmapped_nonzero = Decimal("0")
    unmapped_rows = 0
    for group in report.groups:
        if not group.unmapped:
            continue
        for row in group.details:
            unmapped_rows += 1
            unmapped_nonzero += row.available_ea + row.outbound_7d_ea
    checks.append(
        _check(
            "unmapped_nonzero_rows",
            unmapped_nonzero == 0,
            expected=Decimal("0"),
            actual=unmapped_nonzero,
            severity="blocking",
            message=(
                f"{unmapped_rows} detail row(s) have no belonging_center mapping"
                if unmapped_rows and unmapped_nonzero
                else "zero-valued unmapped rows are retained" if unmapped_rows else ""
            ),
        )
    )

    unknown_count = len(report.unknown_type_codes)
    checks.append(
        _check(
            "known_warehouse_layer_type",
            unknown_count == 0,
            expected=Decimal("0"),
            actual=Decimal(unknown_count),
            severity="blocking",
            message=(
                "unknown wms_node/wms_type for warehouse code(s): "
                + ", ".join(report.unknown_type_codes)
                if unknown_count
                else ""
            ),
        )
    )

    def freshness_check(
        name: str,
        rows: Sequence[TurnoverRow],
        attribute: str,
        *,
        measure_attribute: str,
        require_in_window: bool = False,
    ) -> ReconciliationCheck:
        measured = [row for row in rows if getattr(row, measure_attribute) > 0]
        if not measured:
            return _check(
                name,
                True,
                severity="not_applicable",
                message="no non-zero measure in this layer",
            )
        missing = [row.wms_code for row in measured if getattr(row, attribute) in (None, "")]
        invalid = []
        outside = []
        latest: Any = None
        for row in measured:
            value = getattr(row, attribute)
            if value in (None, ""):
                continue
            try:
                parsed = _parse_datetime(value, timezone_name=report.timezone_name)
            except TurnoverReportError:
                invalid.append(row.wms_code)
                continue
            if latest is None or parsed > latest:
                latest = parsed
            if require_in_window and not (report.window_start <= parsed < report.window_end_exclusive):
                outside.append(row.wms_code)
        passed = not missing and not invalid and not outside
        problems: list[str] = []
        if missing:
            problems.append("missing=" + ",".join(missing))
        if invalid:
            problems.append("invalid=" + ",".join(invalid))
        if outside:
            problems.append("outside_window=" + ",".join(outside))
        message = "; ".join(problems)
        if latest is not None:
            message = (message + "; " if message else "") + f"latest={latest.isoformat()}"
        return _check(name, passed, severity="blocking", message=message)

    checks.append(
        (
            _check(
                "stock_freshness",
                True,
                severity="information",
                message="table-level inventory freshness is validated by the runtime source-quality gate",
            )
            if report.inventory_freshness_mode == "table_update_time"
            else freshness_check(
                "stock_freshness",
                report.source_rows,
                "source_max_stock_change_time",
                measure_attribute="available_ea",
            )
        )
    )
    checks.append(
        freshness_check(
            "outbound_freshness",
            report.source_rows,
            "source_max_ship_at",
            measure_attribute="outbound_7d_ea",
            require_in_window=True,
        )
    )

    for group in report.groups:
        # Profiles may use an independent node-10 summary or a reviewed
        # operating-scope rollup.  Validate the selected rule explicitly.
        if group.center_candidates:
            center_layer_total = _aggregate_rows(
                (
                    group.center_candidates + group.details
                    if report.center_summary_mode == "rollup_scope"
                    else group.center_candidates
                ),
                wms_code=f"__center_layer__:{group.center_name}",
                warehouse_name="中心层合计",
                belonging_center=group.center_name,
                wms_type=10,
            )
            checks.append(
                _check(
                    f"center_layer_total:{group.center_name}",
                    _close_enough(group.center.available_ea, center_layer_total.available_ea, tolerance)
                    and _close_enough(group.center.outbound_7d_ea, center_layer_total.outbound_7d_ea, tolerance),
                    severity="blocking",
                    message=(
                        "center summary equals the reviewed center-plus-detail scope"
                        if report.center_summary_mode == "rollup_scope"
                        else "center-layer aggregate preserves the supplied center rows"
                    ),
                )
            )
        else:
            checks.append(
                _check(
                    f"center_layer_derived:{group.center_name}",
                    True,
                    severity="warning",
                    message="center summary is derived because no independent center-layer row was supplied",
                )
            )

        if not group.details:
            checks.append(
                _check(
                    f"detail_present:{group.center_name}",
                    True,
                    severity="warning",
                    message="no subwarehouse detail supplied; center summary is not detail-reconciled",
                )
            )
            continue
        # The detail rows have no second independent source in this contract.
        # Their authoritative total is checked against the SQL control totals
        # by ``TurnoverReportRuntime``; do not report a self-comparison here as
        # if it were an independent reconciliation.
        checks.append(
            _check(
                f"detail_layer_total:{group.center_name}",
                True,
                severity="information",
                message="detail-layer total is validated by the SQL control total at runtime",
            )
        )
        checks.append(
            _check(
                f"cross_layer:{group.center_name}",
                True,
                severity="information" if report.center_summary_mode == "rollup_scope" else "not_applicable",
                message=(
                    "center summary includes its center warehouse and governed detail scope"
                    if report.center_summary_mode == "rollup_scope"
                    else "center and detail layers are independent statistics; cross-layer equality is not required"
                ),
            )
        )

    center_total = report.center_total
    if center_total is not None and report.detail_total is not None:
        checks.append(
            _check(
                "cross_layer_grand_total",
                True,
                severity="not_applicable",
                message="center and detail grand totals are not compared because they are different statistical layers",
            )
        )

    if center_total is not None:
        expected_days = center_total.turnover_days_for(report.window_days)
        # Recompute from raw totals, rather than averaging center ratios.
        recomputed_days = (
            center_total.available_ea
            / (center_total.outbound_7d_ea / Decimal(report.window_days))
            if center_total.outbound_7d_ea
            else None
        )
        ratio_passed = expected_days is None and recomputed_days is None
        if expected_days is not None and recomputed_days is not None:
            ratio_passed = _close_enough(expected_days, recomputed_days, tolerance)
        checks.append(
            _check(
                "grand_turnover_formula",
                ratio_passed,
                severity="blocking",
                message="total turnover is total inventory divided by total daily outbound; ratios are never averaged",
            )
        )
    return ReconciliationResult(tuple(checks))


def format_quantity(value: Decimal | int | str | float) -> str:
    """Format an EA quantity for the image without binary-float rounding."""

    number = _decimal(value, "quantity")
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def format_days(value: Decimal | None) -> str:
    if value is None:
        return NO_MOVEMENT_LABEL
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def _sql_literal(value: str) -> str:
    text = _normalise_text(value, "cargo_owner_id")
    if len(text) > 64 or not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        raise TurnoverReportError("cargo_owner_id contains unsupported characters")
    return "'" + text.replace("'", "''") + "'"


def build_turnover_sql(
    start_inclusive: Any,
    end_exclusive: Any,
    cargo_owner_id: str = "HJXH",
    occupation_lookback_days: int = 60,
    warehouse_types: Sequence[int] = (10, 11),
    *,
    inventory_basis: str = "actual_quantity",
    inventory_status: str = "ZP_OR_EMPTY",
    window_days: int = TURNOVER_WINDOW_DAYS,
) -> str:
    """Return a single read-only Doris query for warehouse turnover aggregates.

    Dates are rendered as validated local ``Asia/Shanghai`` literals so the
    resulting statement can be handed directly to the read-only Doris
    executor. The warehouse dimension uses node-10 self keys for centers and
    resolves node12/13 details by stable explicit/senior parent codes; it never
    performs fuzzy name matching.
    """

    start_dt, end_dt, _ = _window_bounds(
        start_inclusive, end_exclusive, required_days=window_days
    )
    if (
        not isinstance(occupation_lookback_days, int)
        or isinstance(occupation_lookback_days, bool)
        or occupation_lookback_days <= 0
        or occupation_lookback_days > 3650
    ):
        raise TurnoverReportError("occupation_lookback_days must be between 1 and 3650")
    if not warehouse_types:
        raise TurnoverReportError("warehouse_types cannot be empty")
    normal_types: list[int] = []
    for value in warehouse_types:
        if isinstance(value, bool):
            raise TurnoverReportError("warehouse_types must contain integers")
        try:
            item = int(value)
        except (TypeError, ValueError) as exc:
            raise TurnoverReportError("warehouse_types must contain integers") from exc
        if item < 0 or item > 999:
            raise TurnoverReportError("warehouse_types contains an invalid value")
        normal_types.append(item)
    types_sql = ", ".join(str(item) for item in dict.fromkeys(normal_types))
    owner_sql = _sql_literal(cargo_owner_id)
    start_literal = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_literal = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    inventory_basis = str(inventory_basis or "").strip().casefold()
    if inventory_basis not in {"actual_quantity", "record_deductions"}:
        raise TurnoverReportError("inventory_basis must be actual_quantity or record_deductions")
    inventory_status = str(inventory_status or "").strip()
    if not inventory_status:
        raise TurnoverReportError("inventory_status is required")
    if len(inventory_status) > 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", inventory_status):
        raise TurnoverReportError("inventory_status contains unsupported characters")
    status_key = inventory_status.upper()
    if status_key == "ALL":
        status_sql = ""
    elif status_key in {"ZP_OR_EMPTY", "ZP_OR_NULL", "ZP_OR_BLANK"}:
        # The source-project HJXH rule explicitly includes NULL and blank
        # quality statuses. Treating them as excluded would undercount current
        # inventory while looking superficially like a valid query.
        status_sql = "\n      AND COALESCE(TRIM(s.inventory_status), '') IN ('', 'ZP')"
    else:
        status_sql = (
            f"\n      AND COALESCE(TRIM(s.inventory_status), '') = {_sql_literal(inventory_status)}"
        )
    if inventory_basis == "actual_quantity":
        stock_value_sql = "IFNULL(s.actual_quantity, 0)"
        stock_aggregate_alias = "actual_ea"
        occupation_cte = ""
        occupation_join = ""
        inventory_value_expression = "GREATEST(s.actual_ea, 0)"
        negative_stock_expression = "CASE WHEN s.actual_ea < 0 THEN 1 ELSE 0 END"
    else:
        stock_value_sql = (
            "IFNULL(s.in_stock_quantity, 0) - IFNULL(s.allot_quantity, 0) "
            "- IFNULL(s.lock_quantity, 0) - IFNULL(s.freeze_quantity, 0) "
            "+ IFNULL(s.move_in_quantity, 0) - IFNULL(s.move_out_quantity, 0)"
        )
        stock_aggregate_alias = "record_ea"
        occupation_cte = f""", occupation_by_sku AS (
    SELECT
        TRIM(o.cargo_owner_id) AS cargo_owner_id,
        TRIM(o.wms_code) AS wms_code,
        TRIM(o.sku_code) AS sku_code,
        SUM(IFNULL(o.lock_quantity, 0) + IFNULL(o.freeze_quantity, 0)) AS occupation_qty
    FROM ywzt_ods.scm_warehous_occupy AS o
    JOIN warehouse_scope AS w
      ON w.wms_code = TRIM(o.wms_code)
      AND w.cargo_owner_id = TRIM(o.cargo_owner_id)
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(o.sku_code)
    WHERE TRIM(o.cargo_owner_id) = {owner_sql}
      AND (o.status <> '1' OR o.status IS NULL)
      AND o.created_date >= DATE_SUB('{end_literal}', INTERVAL {occupation_lookback_days} DAY)
      AND o.created_date < '{end_literal}'
    GROUP BY TRIM(o.cargo_owner_id), TRIM(o.wms_code), TRIM(o.sku_code)
)"""
        occupation_join = """LEFT JOIN occupation_by_sku AS o
      ON o.cargo_owner_id = s.cargo_owner_id
     AND o.wms_code = s.wms_code
     AND o.sku_code = s.sku_code"""
        inventory_value_expression = "GREATEST(s.record_ea - IFNULL(o.occupation_qty, 0), 0)"
        negative_stock_expression = "CASE WHEN s.record_ea - IFNULL(o.occupation_qty, 0) < 0 THEN 1 ELSE 0 END"
    return f"""-- Turnover contract: {window_days} complete natural days; quantities are EA.
-- Window: [{start_literal}, {end_literal}) Asia/Shanghai.
WITH warehouse_scope_raw AS (
    SELECT
        TRIM(wms_code) AS wms_code,
        TRIM(COALESCE(wms_desc, '')) AS wms_desc,
        TRIM(CAST(wms_node AS STRING)) AS wms_node,
        TRIM(CAST(wms_type AS STRING)) AS wms_type,
        wms_status,
        status,
        TRIM(cargo_owner_id) AS cargo_owner_id,
        updated_date,
        NULLIF(TRIM(belonging_center), '') AS explicit_center_code,
        NULLIF(TRIM(senior_wms), '') AS senior_center_code,
         CASE
             WHEN TRIM(CAST(wms_node AS STRING)) = '10'
                  AND TRIM(belonging_center) = TRIM(wms_code)
                 THEN TRIM(wms_code)
             WHEN NULLIF(TRIM(belonging_center), '') IS NOT NULL
                 THEN TRIM(belonging_center)
             WHEN NULLIF(TRIM(senior_wms), '') IS NOT NULL
                 THEN TRIM(senior_wms)
             ELSE NULL
         END AS candidate_center_code
    FROM ywzt_ods.master_wms
    WHERE TRIM(cargo_owner_id) = {owner_sql}
      AND TRIM(CAST(wms_node AS STRING)) IN ('10', '12', '13')
      AND COALESCE(CAST(status AS STRING), '0') = '0'
      AND COALESCE(CAST(wms_status AS STRING), '10') = '10'
      AND COALESCE(wms_desc, '') NOT LIKE '%残次%'
      AND COALESCE(wms_desc, '') NOT LIKE '%次品%'
      AND COALESCE(wms_desc, '') NOT LIKE '%废品%'
      AND COALESCE(wms_desc, '') NOT LIKE '%报废%'
      AND COALESCE(wms_desc, '') NOT LIKE '%残品%'
      AND COALESCE(wms_desc, '') NOT LIKE '%样品%'
      AND COALESCE(wms_desc, '') NOT LIKE '%包材%'
      AND COALESCE(wms_desc, '') NOT LIKE '%工厂%'
      AND COALESCE(wms_desc, '') NOT LIKE '%门店%'
       AND TRIM(CAST(wms_type AS STRING)) IN ({", ".join(repr(str(item)) for item in dict.fromkeys(normal_types))})
 ), warehouse_scope_conflicts AS (
    SELECT
        cargo_owner_id,
        wms_code,
        COUNT(DISTINCT CONCAT_WS('|',
            COALESCE(wms_desc, ''),
            COALESCE(wms_node, ''),
            COALESCE(wms_type, ''),
            COALESCE(candidate_center_code, ''),
            COALESCE(senior_center_code, ''),
            COALESCE(CAST(wms_status AS STRING), ''),
            COALESCE(CAST(status AS STRING), '')
        )) AS mapping_variant_count
    FROM warehouse_scope_raw
    GROUP BY cargo_owner_id, wms_code
 ), warehouse_scope_latest AS (
    SELECT
        r.*,
        ROW_NUMBER() OVER (
            PARTITION BY r.cargo_owner_id, r.wms_code
            ORDER BY r.updated_date DESC, r.wms_desc, r.candidate_center_code, r.wms_code
        ) AS warehouse_rank
    FROM warehouse_scope_raw AS r
 ), warehouse_scope_resolved AS (
    SELECT *
    FROM (
        SELECT
            d.*,
            c.mapping_variant_count,
             CASE
                 WHEN d.wms_node = '10'
                     THEN d.wms_code
                 WHEN d.wms_node IN ('12', '13')
                      AND p.wms_code IS NOT NULL
                      AND p.wms_node = '10'
                     THEN d.candidate_center_code
                 ELSE NULL
             END AS belonging_center_code,
             CASE
                 WHEN d.wms_node = '10'
                     THEN d.wms_desc
                 WHEN d.wms_node IN ('12', '13')
                      AND p.wms_code IS NOT NULL
                      AND p.wms_node = '10'
                     THEN p.wms_desc
                 ELSE NULL
             END AS belonging_center,
             CASE
                 WHEN d.wms_node IN ('12', '13')
                      AND (
                          d.candidate_center_code IS NULL
                          OR p.wms_code IS NULL
                          OR p.wms_node <> '10'
                      )
                     THEN 1
                 ELSE 0
             END AS invalid_parent_mapping
               , CASE
                 WHEN d.wms_node = '10'
                      AND (
                          d.explicit_center_code IS NULL
                          OR d.senior_center_code IS NULL
                          OR d.explicit_center_code <> d.wms_code
                          OR d.senior_center_code <> d.wms_code
                          OR d.explicit_center_code <> d.senior_center_code
                      )
                     THEN 1
                 ELSE 0
               END AS center_parent_anomaly
        FROM warehouse_scope_latest AS d
        LEFT JOIN warehouse_scope_latest AS p
          ON p.cargo_owner_id = d.cargo_owner_id
         AND p.wms_code = d.candidate_center_code
         AND p.warehouse_rank = 1
        LEFT JOIN warehouse_scope_conflicts AS c
          ON c.cargo_owner_id = d.cargo_owner_id
         AND c.wms_code = d.wms_code
        WHERE d.warehouse_rank = 1
    ) AS resolved
 ), warehouse_scope AS (
    SELECT *
    FROM warehouse_scope_resolved
), warehouse_quality AS (
    SELECT
        COUNT(*) AS warehouse_scope_row_count,
        COALESCE(SUM(CASE WHEN wms_node = '10' THEN 1 ELSE 0 END), 0) AS warehouse_center_row_count,
        COALESCE(SUM(CASE WHEN wms_node IN ('12', '13') AND belonging_center_code IS NOT NULL THEN 1 ELSE 0 END), 0) AS warehouse_detail_row_count,
        COALESCE(SUM(CASE WHEN mapping_variant_count > 1 THEN 1 ELSE 0 END), 0) AS warehouse_mapping_conflict_count,
        COALESCE(SUM(CASE WHEN invalid_parent_mapping > 0 THEN 1 ELSE 0 END), 0) AS invalid_parent_mapping_count,
        COALESCE(SUM(CASE WHEN belonging_center_code IS NULL THEN 1 ELSE 0 END), 0) AS unresolved_mapping_count,
        COALESCE(SUM(center_parent_anomaly), 0) AS center_parent_anomaly_count,
        MAX(updated_date) AS source_table_max_wms_updated_date
    FROM warehouse_scope
), sku_scope AS (
    SELECT TRIM(sku_code) AS sku_code
    FROM ywzt_ods.master_sku
    WHERE sku_group = '50'
    GROUP BY TRIM(sku_code)
), stock_raw_by_sku AS (
    SELECT
        TRIM(s.cargo_owner_id) AS cargo_owner_id,
        TRIM(s.wms_code) AS wms_code,
        TRIM(s.sku_code) AS sku_code,
        SUM({stock_value_sql}) AS {stock_aggregate_alias},
        MAX(s.stock_change_time) AS source_max_stock_change_time,
        COUNT(*) AS inventory_row_count
    FROM ywzt_ods.scm_warehous_manage AS s
    JOIN warehouse_scope AS w
      ON w.wms_code = TRIM(s.wms_code)
      AND w.cargo_owner_id = TRIM(s.cargo_owner_id)
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(s.sku_code)
    WHERE COALESCE(CAST(s.status AS STRING), '0') = '0'
      AND TRIM(s.cargo_owner_id) = {owner_sql}
      {status_sql}
    GROUP BY TRIM(s.cargo_owner_id), TRIM(s.wms_code), TRIM(s.sku_code)
), stock_by_sku AS (
    SELECT r.*
    FROM stock_raw_by_sku AS r
){occupation_cte}
, inventory_by_warehouse AS (
    SELECT
        s.cargo_owner_id,
        s.wms_code,
    SUM({inventory_value_expression}) AS available_ea,
        MAX(s.source_max_stock_change_time) AS source_max_stock_change_time,
        SUM(s.inventory_row_count) AS inventory_row_count,
        SUM({negative_stock_expression}) AS negative_stock_count
    FROM stock_by_sku AS s
    {occupation_join}
    GROUP BY s.cargo_owner_id, s.wms_code
), eligible_outbound_raw AS (
    SELECT
        TRIM(h.outbound_code) AS outbound_code,
        TRIM(h.company_code) AS cargo_owner_id,
        TRIM(h.facility_code) AS wms_code,
        h.ship_at,
        h.ref_order_code,
        h.ref_order_type
    FROM ywzt_ods.scm_sales_shipment_header AS h
    JOIN warehouse_scope AS w
      ON w.wms_code = TRIM(h.facility_code)
     AND w.cargo_owner_id = TRIM(h.company_code)
    WHERE h.ship_at >= '{start_literal}'
      AND h.ship_at < '{end_literal}'
      AND TRIM(h.company_code) = {owner_sql}
           AND {completed_status_sql()}
      AND COALESCE(CAST(h.delete_flag AS STRING), '0') = '0'
      AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
      AND (
          h.ref_order_type = 'DBCK'
          OR (
              h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK')
              OR h.ref_order_type LIKE 'SO%'
          )
          AND EXISTS (
              SELECT 1
              FROM ywzt_ods.scm_sales_order_header AS so
              WHERE so.sales_order_code = h.ref_order_code
                AND TRIM(so.company_code) = TRIM(h.company_code)
                AND COALESCE(CAST(so.deleted_flag AS STRING), '0') = '0'
                AND IFNULL(so._fdl_marked_deleted, 'false') = 'false'
          )
      )
), outbound_grouped AS (
    SELECT
        outbound_code,
        cargo_owner_id,
        MIN(wms_code) AS wms_code,
        MAX(ship_at) AS source_max_ship_at,
        COUNT(DISTINCT wms_code) AS facility_variant_count
    FROM eligible_outbound_raw
    GROUP BY outbound_code, cargo_owner_id
), eligible_outbound AS (
    SELECT outbound_code, cargo_owner_id, wms_code, source_max_ship_at
    FROM outbound_grouped
    WHERE facility_variant_count = 1
), outbound_by_warehouse AS (
    SELECT
        e.cargo_owner_id,
        e.wms_code,
        SUM(IFNULL(d.ship_qty, 0)) AS outbound_7d_ea,
        MAX(e.source_max_ship_at) AS source_max_ship_at,
        COUNT(*) AS outbound_row_count,
        SUM(CASE WHEN IFNULL(d.ship_qty, 0) < 0 THEN 1 ELSE 0 END) AS negative_outbound_count
    FROM eligible_outbound AS e
    JOIN ywzt_ods.scm_sales_shipment_detail AS d
      ON TRIM(d.outbound_code) = e.outbound_code
      AND TRIM(d.company_code) = e.cargo_owner_id
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(d.sku_code)
    WHERE COALESCE(CAST(d.delete_flag AS STRING), '0') = '0'
      AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
    GROUP BY e.cargo_owner_id, e.wms_code
), inventory_control AS (
    SELECT
        COALESCE(SUM({inventory_value_expression}), 0) AS control_inventory_total_ea,
        COALESCE(SUM(s.inventory_row_count), 0) AS control_inventory_row_count,
        COALESCE(SUM({negative_stock_expression}), 0) AS control_negative_stock_count
    FROM stock_by_sku AS s
    {occupation_join}
), outbound_control AS (
    SELECT
        COALESCE(SUM(IFNULL(d.ship_qty, 0)), 0) AS control_outbound_total_ea,
        COUNT(*) AS control_outbound_row_count,
        COALESCE(SUM(CASE WHEN IFNULL(d.ship_qty, 0) < 0 THEN 1 ELSE 0 END), 0) AS control_negative_outbound_count
    FROM eligible_outbound AS e
    JOIN ywzt_ods.scm_sales_shipment_detail AS d
      ON TRIM(d.outbound_code) = e.outbound_code
      AND TRIM(d.company_code) = e.cargo_owner_id
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(d.sku_code)
    WHERE COALESCE(CAST(d.delete_flag AS STRING), '0') = '0'
      AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
), stock_source_quality AS (
    SELECT
        MAX(s.stock_change_time) AS source_table_max_stock_change_time,
        COUNT(*) AS source_table_inventory_row_count
    FROM ywzt_ods.scm_warehous_manage AS s
    JOIN warehouse_scope AS w
      ON w.wms_code = TRIM(s.wms_code)
      AND w.cargo_owner_id = TRIM(s.cargo_owner_id)
    JOIN sku_scope AS ss
      ON ss.sku_code = TRIM(s.sku_code)
    WHERE COALESCE(CAST(s.status AS STRING), '0') = '0'
      AND TRIM(s.cargo_owner_id) = {owner_sql}
      {status_sql}
), outbound_source_quality AS (
    SELECT
        MAX(source_max_ship_at) AS source_table_max_ship_at,
        COUNT(*) AS source_table_outbound_header_count,
        COALESCE(SUM(CASE WHEN facility_variant_count > 1 THEN 1 ELSE 0 END), 0) AS ambiguous_outbound_count
    FROM outbound_grouped
), warehouse_keys AS (
    SELECT cargo_owner_id, wms_code FROM warehouse_scope
)
SELECT
    k.cargo_owner_id,
    k.wms_code,
    mw.wms_desc AS warehouse_name,
    mw.wms_type,
    mw.wms_node,
    mw.belonging_center_code,
    mw.belonging_center,
    IFNULL(i.available_ea, 0) AS available_ea,
    IFNULL(o.outbound_7d_ea, 0) AS outbound_7d_ea,
    IFNULL(i.inventory_row_count, 0) AS inventory_row_count,
    IFNULL(i.negative_stock_count, 0) AS negative_stock_count,
    IFNULL(o.outbound_row_count, 0) AS outbound_row_count,
    IFNULL(o.negative_outbound_count, 0) AS negative_outbound_count,
    i.source_max_stock_change_time,
    o.source_max_ship_at,
    stock_quality.source_table_max_stock_change_time,
    outbound_quality.source_table_max_ship_at,
    outbound_quality.ambiguous_outbound_count,
    stock_quality.source_table_inventory_row_count,
    outbound_quality.source_table_outbound_header_count,
    warehouse_quality.warehouse_scope_row_count,
    warehouse_quality.warehouse_center_row_count,
    warehouse_quality.warehouse_detail_row_count,
    warehouse_quality.warehouse_mapping_conflict_count,
    warehouse_quality.invalid_parent_mapping_count,
    warehouse_quality.unresolved_mapping_count,
    warehouse_quality.center_parent_anomaly_count,
    warehouse_quality.source_table_max_wms_updated_date,
    inventory_control.control_inventory_total_ea,
    outbound_control.control_outbound_total_ea,
    inventory_control.control_inventory_row_count,
    outbound_control.control_outbound_row_count,
    inventory_control.control_negative_stock_count,
    outbound_control.control_negative_outbound_count
FROM warehouse_keys AS k
JOIN warehouse_scope AS mw
  ON mw.wms_code = k.wms_code AND mw.cargo_owner_id = k.cargo_owner_id
LEFT JOIN inventory_by_warehouse AS i
  ON i.cargo_owner_id = k.cargo_owner_id AND i.wms_code = k.wms_code
LEFT JOIN outbound_by_warehouse AS o
  ON o.cargo_owner_id = k.cargo_owner_id AND o.wms_code = k.wms_code
LEFT JOIN stock_source_quality AS stock_quality ON 1 = 1
LEFT JOIN outbound_source_quality AS outbound_quality ON 1 = 1
LEFT JOIN warehouse_quality ON 1 = 1
LEFT JOIN inventory_control ON 1 = 1
LEFT JOIN outbound_control ON 1 = 1
ORDER BY mw.wms_node, mw.wms_type, k.wms_code"""


# ---------------------------------------------------------------------------
# PIL renderer


def _load_pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise TurnoverReportError(
            "Pillow is required to render turnover PNGs; install the Pillow package"
        ) from exc
    return Image, ImageDraw, ImageFont


def _load_brand_logo(Image: Any, logo_path: str | Path | None = None) -> Any:
    """Load the supplied Royal Little Tiger logo for report identity.

    The report is a branded business artifact as well as a data output.  Use
    the checked-in source asset, crop only its transparent padding, and keep
    the original aspect ratio; never synthesize a replacement mark.
    """

    candidate = Path(logo_path) if logo_path else (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "assets"
        / "royal-tiger"
        / "royal-tiger-logo-banner.png"
    )
    if not candidate.is_file():
        raise TurnoverReportError("Royal Little Tiger logo asset is required")
    try:
        with Image.open(candidate) as source:
            logo = source.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise TurnoverReportError("Royal Little Tiger logo asset is invalid") from exc
    bbox = logo.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    if logo.width <= 0 or logo.height <= 0:
        raise TurnoverReportError("Royal Little Tiger logo asset is empty")
    return logo


def _font_path_candidates(bold: bool) -> tuple[Path, ...]:
    names = (
        ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc")
        if bold
        else ("msyh.ttc", "simhei.ttf", "simsun.ttc")
    )
    directories = (
        Path(r"C:\Windows\Fonts"),
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/truetype/wqy"),
        Path("/usr/share/fonts/truetype/arphic"),
    )
    candidates: list[Path] = []
    for directory in directories:
        for name in names:
            candidates.append(directory / name)
    return tuple(candidates)


def _get_font(ImageFont: Any, size: int, *, bold: bool = False, font_path: str | Path | None = None) -> Any:
    candidates = ((Path(font_path),) if font_path else ()) + _font_path_candidates(bold)
    for path in candidates:
        try:
            if path.exists():
                return ImageFont.truetype(str(path), size=size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow versions before the sized default font
        return ImageFont.load_default()


def _text_size(draw: Any, text: str, font: Any, *, spacing: int = 2) -> tuple[int, int]:
    box = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
    return max(0, box[2] - box[0]), max(0, box[3] - box[1])


def _wrap_text(draw: Any, text: str, font: Any, max_width: int) -> str:
    text = str(text)
    if not text:
        return ""
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines) or text


def _draw_center(draw: Any, box: tuple[int, int, int, int], text: str, font: Any, fill: str) -> None:
    x1, y1, x2, y2 = box
    width, height = _text_size(draw, text, font)
    draw.multiline_text(
        (x1 + (x2 - x1 - width) / 2, y1 + (y2 - y1 - height) / 2),
        text,
        font=font,
        fill=fill,
        spacing=2,
        align="center",
    )


def _draw_left(draw: Any, box: tuple[int, int, int, int], text: str, font: Any, fill: str) -> None:
    x1, y1, x2, y2 = box
    width, height = _text_size(draw, text, font)
    draw.multiline_text(
        (x1 + 10, y1 + (y2 - y1 - height) / 2),
        text,
        font=font,
        fill=fill,
        spacing=2,
        align="left",
    )


def _quality_display(value: str | None, report: TurnoverReport) -> str:
    status = str(value or report.quality_status).strip().casefold()
    if status in {"passed_with_warning", "usable_with_warning"}:
        return "已通过（有告警）"
    if status in {"passed", "usable"}:
        return "已通过"
    return "未通过"




def _render_turnover_image(
    report: TurnoverReport,
    *,
    font_path: str | Path | None = None,
    brand_logo_path: str | Path | None = None,
    width: int = 1800,
    detail_columns: int = 3,
    reference_style: bool = False,
    quality_status: str | None = None,
    center_report: TurnoverReport | None = None,
    template: Any = None,
) -> Any:
    Image, ImageDraw, ImageFont = _load_pillow()
    if width < 900:
        raise TurnoverReportError("renderer width must be at least 900 pixels")
    if (
        not isinstance(detail_columns, int)
        or isinstance(detail_columns, bool)
        or detail_columns < 1
        or detail_columns > 4
    ):
        raise TurnoverReportError("detail_columns must be an integer between 1 and 4")
    if reference_style:
        from .turnover_reference_image import render_reference_image
        return render_reference_image(report, center_report=center_report,
                                      template=template, font_path=font_path, width=width,
                                      quality_status=quality_status)
    # Keep the four-column proportions close to the supplied spreadsheet while
    # allowing long warehouse names to wrap over multiple lines.
    margin = 48
    inner_width = width - margin * 2
    ink = "#241C18"
    muted = "#6D625B"
    grid = "#2F2925"
    yellow = "#FFC600"
    pale_yellow = "#FFF4CC"
    paper = "#FFFFFF"
    soft = "#FAF8F5"
    warning = "#9A3D25"

    title_font = _get_font(ImageFont, 32, bold=True, font_path=font_path)
    section_font = _get_font(ImageFont, 23, bold=True, font_path=font_path)
    header_font = _get_font(ImageFont, 18, bold=True, font_path=font_path)
    body_font = _get_font(ImageFont, 18, bold=False, font_path=font_path)
    total_font = _get_font(ImageFont, 19, bold=True, font_path=font_path)
    small_font = _get_font(ImageFont, 15, bold=False, font_path=font_path)
    brand_logo = _load_brand_logo(Image, brand_logo_path)
    title_h = 82
    logo_max_height = title_h
    logo_height = min(58, logo_max_height - 20)
    logo_width = max(1, round(brand_logo.width * logo_height / brand_logo.height))
    resampling = getattr(Image, "Resampling", Image)
    brand_logo = brand_logo.resize((logo_width, logo_height), resampling.LANCZOS)

    columns = (
        ("归属中心仓", 0.34),
        ("实时库存量\n（EA）", 0.22),
        (f"近{report.window_days}天日均动销\n（EA/日）", 0.24),
        ("平均周转天数", 0.20),
    )
    col_widths = [int(inner_width * ratio) for _, ratio in columns]
    col_widths[-1] += inner_width - sum(col_widths)

    def row_height(
        row: TurnoverRow,
        *,
        header: bool = False,
        total: bool = False,
        name_width: int | None = None,
    ) -> int:
        if header:
            wrapped = [label for label, _ in columns]
            max_lines = max(item.count("\n") + 1 for item in wrapped)
            return max(54, 22 * max_lines + 18)
        row_font = total_font if total else body_font
        name = _wrap_text(draw_probe, row.warehouse_name, row_font, (name_width or col_widths[0]) - 20)
        _, name_h = _text_size(draw_probe, name, row_font)
        return max(48 if not total else 52, name_h + (18 if not total else 20))

    # A small measurement canvas lets us calculate every dynamic row before
    # allocating the final image, so the footer is never clipped.
    draw_probe = ImageDraw.Draw(Image.new("RGB", (width, 100), paper))
    center_header_h = row_height(report.center_rows[0] if report.center_rows else TurnoverRow("x", "x", 0, 0), header=True)

    center_rows = report.center_rows
    center_data_heights = [row_height(row, name_width=col_widths[0]) for row in center_rows]
    center_total = report.center_total
    center_total_h = row_height(center_total, total=True, name_width=col_widths[0]) if center_total else 52

    detail_gap = 22
    detail_width = (inner_width - detail_gap * (detail_columns - 1)) // detail_columns
    detail_width_remainder = inner_width - (
        detail_width * detail_columns + detail_gap * (detail_columns - 1)
    )
    detail_local_widths = [int(detail_width * ratio) for _, ratio in columns]
    detail_local_widths[-1] += detail_width - sum(detail_local_widths)
    table_heading_h = 0 if reference_style else 40
    detail_tables: list[tuple[TurnoverGroup, list[int], int]] = []
    for group in report.groups:
        data_heights = [
            row_height(row, name_width=detail_local_widths[0]) for row in group.details
        ]
        detail_total_height = (
            row_height(
                _aggregate_rows(
                    group.details,
                    wms_code=f"__detail_total__:{group.center_name}",
                    warehouse_name="合计",
                    belonging_center=group.center_name,
                    wms_type=11,
                    wms_node=12,
                ),
                total=True,
                name_width=detail_local_widths[0],
            )
            if group.details
            else 0
        )
        # Empty groups still get a stable one-row placeholder.
        table_height = table_heading_h + center_header_h + sum(data_heights or [48]) + max(52, detail_total_height)
        detail_tables.append((group, data_heights, table_height))

    title_h = 82
    metadata_h = 42
    section_h = 42
    footer_h = 48
    center_table_h = table_heading_h + center_header_h + sum(center_data_heights) + center_total_h
    detail_grid_h = 0
    if detail_tables:
        for index in range(0, len(detail_tables), detail_columns):
            detail_grid_h += max(item[2] for item in detail_tables[index : index + detail_columns]) + detail_gap
    else:
        detail_grid_h = 64
    height = margin + title_h + metadata_h + section_h + center_table_h + 28 + section_h + detail_grid_h + footer_h + margin
    image = Image.new("RGB", (width, height), paper)
    draw = ImageDraw.Draw(image)

    # Keep the supplied Royal Little Tiger mark visible once in the header and
    # reserve the remaining space for the report title/profile.
    draw.rectangle((0, 0, width, title_h), fill=yellow)
    logo_y = (title_h - logo_height) // 2
    if image.mode == "RGBA":
        image.alpha_composite(brand_logo, (margin, logo_y))
    else:
        image.paste(brand_logo, (margin, logo_y), brand_logo)
    title = (
        "皇家小虎中心仓周转情况"
        if reference_style
        else f"仓库周转报告｜{report.profile_name}"
    )
    title_left = margin + logo_width + 24
    _draw_left(draw, (title_left, 0, width - margin, title_h), title, title_font, ink)
    metadata = (
        f"统计窗口：{report.window_start:%Y-%m-%d} 00:00 至 "
        f"{report.window_end_exclusive:%Y-%m-%d} 00:00（左闭右开）    "
        f"单位：{report.unit}    质量：{_quality_display(quality_status, report)}"
    )
    _draw_left(draw, (margin, title_h, width - margin, title_h + metadata_h), metadata, small_font, muted)

    def draw_table(
        x: int,
        y: int,
        heading: str,
        rows_to_draw: Sequence[TurnoverRow],
        total_row: TurnoverRow | None,
        *,
        table_width: int = inner_width,
        compact: bool = False,
        first_column_label: str | None = None,
    ) -> int:
        heading_h = table_heading_h
        if heading_h:
            draw.rectangle((x, y, x + table_width, y + heading_h), fill=soft)
            _draw_left(draw, (x, y, x + table_width, y + heading_h), heading, section_font, ink)
        top = y + heading_h
        table_columns = list(columns)
        if reference_style and compact:
            table_columns[0] = (heading, table_columns[0][1])
        elif first_column_label:
            table_columns[0] = (first_column_label, table_columns[0][1])
        local_widths = col_widths if table_width == inner_width else [
            int(table_width * ratio) for _, ratio in columns
        ]
        local_widths[-1] += table_width - sum(local_widths)
        edge_x = [x]
        for item in local_widths:
            edge_x.append(edge_x[-1] + item)
        header_h = max(
            54,
            22 * max(label.count("\n") + 1 for label, _ in table_columns) + 18,
        )
        draw.rectangle((x, top, x + table_width, top + header_h), fill=yellow, outline=grid, width=1)
        for idx, (label, _) in enumerate(table_columns):
            cell = (edge_x[idx], top, edge_x[idx + 1], top + header_h)
            draw.rectangle(cell, fill=yellow, outline=grid, width=1)
            _draw_center(draw, cell, label, header_font, ink)
        cursor = top + header_h

        def draw_data_row(row: TurnoverRow, height_px: int, fill: str, *, total: bool = False) -> None:
            nonlocal cursor
            name = row.warehouse_name
            name_font = total_font if total else body_font
            wrapped_name = _wrap_text(draw, name, name_font, local_widths[0] - 20)
            values = (
                wrapped_name,
                format_quantity(row.available_ea),
                format_quantity(row.average_daily_outbound(report.window_days)),
                format_days(row.turnover_days_for(report.window_days)),
            )
            for idx, value in enumerate(values):
                cell = (edge_x[idx], cursor, edge_x[idx + 1], cursor + height_px)
                draw.rectangle(cell, fill=fill, outline=grid, width=1)
                color = warning if idx == 3 and row.status_for(report.window_days) == TurnoverStatus.NO_MOVEMENT else ink
                if idx == 0:
                    _draw_left(draw, cell, value, name_font, color)
                else:
                    _draw_center(draw, cell, value, name_font, color)
            cursor += height_px

        if rows_to_draw:
            for row in rows_to_draw:
                wrapped = _wrap_text(draw, row.warehouse_name, body_font, local_widths[0] - 20)
                _, text_h = _text_size(draw, wrapped, body_font)
                draw_data_row(row, max(48, text_h + 18), paper)
        else:
            placeholder = TurnoverRow("__empty__", "暂无分仓数据", 0, 0, belonging_center=heading)
            draw_data_row(placeholder, 48, paper)
        if total_row is not None:
            draw_data_row(total_row, max(52, _text_size(draw, total_row.warehouse_name, total_font)[1] + 20), pale_yellow, total=True)
        return cursor

    cursor = margin + title_h + metadata_h
    draw.rectangle((margin, cursor, width - margin, cursor + section_h), fill=paper)
    _draw_left(
        draw,
        (margin, cursor, width - margin, cursor + section_h),
        "1、中心仓周转情况：" if reference_style else "中心仓周转情况",
        section_font,
        "#E60012" if reference_style else ink,
    )
    cursor += section_h
    cursor = draw_table(margin, cursor, "中心仓汇总", center_rows, center_total)
    cursor += 28
    draw.rectangle((margin, cursor, width - margin, cursor + section_h), fill=paper)
    _draw_left(
        draw,
        (margin, cursor, width - margin, cursor + section_h),
        "2、各分仓周转情况：" if reference_style else "各分仓周转情况",
        section_font,
        "#E60012" if reference_style else ink,
    )
    cursor += section_h

    if detail_tables:
        for index in range(0, len(detail_tables), detail_columns):
            row_tables = detail_tables[index : index + detail_columns]
            row_start = cursor
            row_ends: list[int] = []
            for col_index, (group, _, _) in enumerate(row_tables):
                # Put any remainder pixel on the first column so the last
                # table edge stays exactly aligned with the report margin.
                x = margin + col_index * (detail_width + detail_gap) + min(col_index, detail_width_remainder)
                table_width = detail_width + (1 if col_index < detail_width_remainder else 0)
                row_ends.append(
                    draw_table(
                        x,
                        row_start,
                        group.center_name,
                        group.details,
                        _aggregate_rows(
                            group.details,
                            wms_code=f"__detail_total__:{group.center_name}",
                            warehouse_name="合计",
                            belonging_center=group.center_name,
                            wms_type=11,
                        )
                        if group.details
                        else None,
                        table_width=table_width,
                        compact=True,
                        first_column_label="分仓",
                    )
                )
            cursor = max(row_ends) + detail_gap
    else:
        cursor = draw_table(
            margin,
            cursor,
            "分仓明细",
            (),
            None,
            first_column_label="分仓",
        )

    footer = (
        f"口径：{report.profile_name}；公式：库存 ÷（{report.window_days}日有效出库量 ÷ {report.window_days}）；"
        "无动销显示“无动销”，不使用 Excel 错误值。"
    )
    _draw_left(draw, (margin, height - margin - footer_h, width - margin, height - margin), footer, small_font, muted)
    return image


def render_turnover_image(
    report: TurnoverReport,
    *,
    font_path: str | Path | None = None,
    brand_logo_path: str | Path | None = None,
    width: int = 1800,
    detail_columns: int = 3,
    reference_style: bool = False,
    quality_status: str | None = None,
    center_report: TurnoverReport | None = None,
    template: Any = None,
) -> Any:
    """Return a PIL image without writing it to disk."""

    if not isinstance(report, TurnoverReport):
        raise TurnoverReportError("report must be a TurnoverReport")
    return _render_turnover_image(
        report,
        font_path=font_path,
        brand_logo_path=brand_logo_path,
        width=width,
        detail_columns=detail_columns,
        reference_style=reference_style,
        quality_status=quality_status,
        center_report=center_report,
        template=template,
    )


def render_turnover_report(
    report: TurnoverReport,
    output_path: str | Path,
    *,
    font_path: str | Path | None = None,
    brand_logo_path: str | Path | None = None,
    width: int = 1800,
    detail_columns: int = 3,
    reference_style: bool = False,
    quality_status: str | None = None,
) -> Path:
    """Render and save a turnover PNG, returning the resulting path."""

    if not isinstance(report, TurnoverReport):
        raise TurnoverReportError("report must be a TurnoverReport")
    path = Path(output_path)
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise TurnoverReportError("output_path must use a PNG, JPEG, or WEBP extension")
    path.parent.mkdir(parents=True, exist_ok=True)
    image = _render_turnover_image(
        report,
        font_path=font_path,
        brand_logo_path=brand_logo_path,
        width=width,
        detail_columns=detail_columns,
        reference_style=reference_style,
        quality_status=quality_status,
    )
    image.save(path)
    return path


__all__ = [
    "DEFAULT_TIMEZONE",
    "DEFAULT_TOLERANCE",
    "NO_MOVEMENT_LABEL",
    "ReconciliationCheck",
    "ReconciliationResult",
    "TURNOVER_WINDOW_DAYS",
    "TurnoverGroup",
    "TurnoverMetricRow",
    "TurnoverReport",
    "TurnoverReportError",
    "TurnoverRow",
    "TurnoverStatus",
    "build_turnover_report",
    "build_turnover_sql",
    "format_days",
    "format_quantity",
    "render_turnover_image",
    "render_turnover_report",
    "validate_reconciliation",
]
