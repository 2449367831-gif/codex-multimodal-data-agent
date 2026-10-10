"""Deterministic Doris turnover report runtime.

The Codex child can help interpret a request, but it never owns the numbers in
the generated image.  This service builds the governed turnover query, checks
the returned aggregate and source-quality gates, and only then renders an
owner-bound PNG artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .artifacts import ArtifactStore
from .chat_tasks import report_progress
from .knowledge import KnowledgeBase
from .knowledge_activation import active_snapshot
from .query_scope import ScopeResolver
from .codex_mcp_bridge import CodexMcpBridge
from .query_outcomes import DorisResultError, validate_doris_result
from .operating_turnover import build_operating_center_turnover_sql
from .turnover_template import load_turnover_template
from .turnover_report import (
    DEFAULT_TIMEZONE,
    TurnoverReport,
    TurnoverReportError,
    build_turnover_report,
    render_turnover_image,
)


LOGGER = logging.getLogger("doris_query_agent.turnover")
REPORT_KIND = "center_warehouse_turnover_7d"
REPORT_METRIC_ID = "inventory.turnover_7d_realtime"
DEFAULT_PROFILE_ID = "actual_finished_goods"
ALLOW_UNAPPROVED_ENV = "AGENT_ALLOW_UNAPPROVED_CONTRACTS"

_REPORT_DURATION = re.compile(
    r"(?<![\d一二三四五六七八九十两])(?:最近|近|过去)?"
    r"(\d+|[一二三四五六七八九十两]+)(?:个)?(?:完整)?(?:自然)?(?:日|天)"
)


def _duration_number(raw: str) -> int | None:
    if raw.isascii() and raw.isdigit():
        return int(raw) if len(raw) <= 4 else None
    digits = {word: number for number, word in enumerate("一二三四五六七八九", 1)}
    digits["两"] = 2
    if raw in digits:
        return digits[raw]
    if raw.count("十") == 1:
        tens, ones = raw.split("十")
        if (not tens or tens in digits) and (not ones or ones in digits):
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None


def report_window_days(question: str) -> int | None:
    compact = "".join(question.split()).lower()
    matches = list(_REPORT_DURATION.finditer(compact))
    if len(matches) != 1:
        return None
    days = _duration_number(matches[0].group(1))
    return days if days and 1 <= days <= 3650 else None


def is_standard_center_report(question: str) -> bool:
    compact = "".join(question.split()).lower()
    scope, shape, _ = ScopeResolver().resolve(question)
    residue = shape
    residue = _REPORT_DURATION.sub("", residue)
    for word in ("中心仓", "分仓", "数据图片", "报表图", "图片", "报表", "当前", "实时", "实际", "成品", "库存", "周转", "生成", "请", "帮我", "近", "最近", "和", "与", "及", "的", "，", "。"):
        residue = residue.replace(word, "")
    return (
        all(word in compact for word in ("中心仓", "分仓", "周转"))
        and any(word in compact for word in ("图片", "报表图", "周转图", "生成图"))
        and report_window_days(question) is not None
        and not any(word in compact for word in ("月", "去年", "在途", "扣减", "渠道", "排除"))
        and not scope.warehouse_codes and not scope.sku_codes and not scope.product_terms
        and not residue
    )


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a fail-closed boolean environment flag."""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class TurnoverProfile:
    profile_id: str
    display_name: str
    inventory_basis: str
    inventory_status: str
    include_cloud: bool = False
    inventory_freshness_mode: str = "row_change_time"
    center_summary_mode: str = "independent"
    operating_scope: bool = False
    require_catalog_freshness: bool = False
    negative_inventory_policy: str = "block"
    missing_inventory_policy: str = "block"


TURNOVER_PROFILES: dict[str, TurnoverProfile] = {
    "available_plus_transit_finished_goods": TurnoverProfile(
        profile_id="available_plus_transit_finished_goods",
        display_name="可用库存 + 在途库存（产成品；8个经营中心）",
        inventory_basis="available_plus_transit",
        inventory_status="ALL",
        inventory_freshness_mode="table_update_time",
        center_summary_mode="rollup_scope",
        operating_scope=True,
        require_catalog_freshness=True,
        # Negative source facts are retained in the net aggregate and surfaced
        # as a quality warning; they are never clamped or silently dropped.
        negative_inventory_policy="retain_warn",
        missing_inventory_policy="block",
    ),
    "actual_finished_goods": TurnoverProfile(
        profile_id="actual_finished_goods",
        display_name="实时可用成品库存 + 在途（8个经营中心）",
        inventory_basis="available_plus_transit",
        inventory_status="ALL",
        # The approved component SQL uses current stock, occupation and transit.
        # Freshness must cover these inputs, not the retired snapshot table.
        inventory_freshness_mode="table_update_time",
        center_summary_mode="rollup_scope",
        operating_scope=True,
        require_catalog_freshness=True,
        negative_inventory_policy="retain_warn",
        missing_inventory_policy="zero_warn",
    ),
    "record_deduction_all": TurnoverProfile(
        profile_id="record_deduction_all",
        display_name="记录库存扣减后（全库存状态；8个经营中心）",
        inventory_basis="record_deductions",
        inventory_status="ALL",
        inventory_freshness_mode="row_change_time",
        center_summary_mode="rollup_scope",
        operating_scope=True,
        require_catalog_freshness=True,
        negative_inventory_policy="retain_warn",
        missing_inventory_policy="zero_warn",
    ),
}


class QueryClient(Protocol):
    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, Any]: ...


class TurnoverRuntimeError(RuntimeError):
    """Internal failure with a stable, non-sensitive public code."""

    def __init__(self, code: str, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.reason = reason


def _business_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TurnoverRuntimeError(
            "report_configuration_error", "报表时区配置无效", reason="timezone"
        ) from exc


def _parse_end_date(value: Any, *, timezone_name: str, now: datetime) -> date:
    if value is None or str(value).strip() == "":
        return now.astimezone(_business_tz(timezone_name)).date()
    if not isinstance(value, str):
        raise TurnoverRuntimeError(
            "report_parameters_invalid", "报表日期格式无效", reason="end_date_type"
        )
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise TurnoverRuntimeError(
            "report_parameters_invalid", "报表日期格式无效", reason="end_date_format"
        ) from exc
    local_today = now.astimezone(_business_tz(timezone_name)).date()
    if parsed > local_today:
        raise TurnoverRuntimeError(
            "report_parameters_invalid", "报表日期不能晚于当前业务日期", reason="future_end_date"
        )
    if parsed < local_today - timedelta(days=3650):
        raise TurnoverRuntimeError(
            "report_parameters_invalid", "报表日期超出可查询范围", reason="end_date_range"
        )
    return parsed


def complete_window(
    end_date_exclusive: Any = None,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
    window_days: int = 7,
) -> tuple[datetime, datetime]:
    """Return the requested complete local days ending at ``end_date_exclusive``."""

    if type(window_days) is not int or not 1 <= window_days <= 3650:
        raise TurnoverRuntimeError("report_parameters_invalid", "周转统计天数必须是1至3650个完整自然日", reason="window_days")

    tz = _business_tz(timezone_name)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    end_date = _parse_end_date(end_date_exclusive, timezone_name=timezone_name, now=current)
    start_date = end_date - timedelta(days=window_days)
    return (
        datetime.combine(start_date, time.min, tzinfo=tz),
        datetime.combine(end_date, time.min, tzinfo=tz),
    )


def _row_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _parse_source_timestamp(value: Any, timezone_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    tz = _business_tz(timezone_name)
    return result.replace(tzinfo=tz) if result.tzinfo is None else result.astimezone(tz)


class TurnoverReportRuntime:
    """Execute, validate and render one governed turnover report."""

    def __init__(
        self,
        *,
        backend_url: str = "http://127.0.0.1:8010",
        artifact_store: ArtifactStore,
        project_root: Path | None = None,
        knowledge: KnowledgeBase | None = None,
        allow_unapproved_contracts: bool | None = None,
        query_client: QueryClient | None = None,
        timezone_name: str = DEFAULT_TIMEZONE,
        cargo_owner_id: str = "HJXH",
        freshness_lag_hours: int = 72,
        clock: Any = datetime.now,
    ) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.artifact_store = artifact_store
        self.project_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
        self.knowledge = knowledge
        # An injected KnowledgeBase is an explicit test/request authority.
        # Production runtimes must resolve the active release on every report
        # so activation, rollback, or revocation cannot be hidden by a stale
        # process-local snapshot.
        self._knowledge_injected = knowledge is not None
        if allow_unapproved_contracts is None:
            self.allow_unapproved_contracts = _env_bool(ALLOW_UNAPPROVED_ENV)
        elif isinstance(allow_unapproved_contracts, bool):
            self.allow_unapproved_contracts = allow_unapproved_contracts
        else:
            raise ValueError("allow_unapproved_contracts must be a boolean")
        self.timezone_name = timezone_name
        self.cargo_owner_id = cargo_owner_id
        if freshness_lag_hours < 1 or freshness_lag_hours > 24 * 30:
            raise ValueError("freshness_lag_hours is outside the supported range")
        self.freshness_lag_hours = int(freshness_lag_hours)
        self._clock = clock
        self.query_client = query_client or CodexMcpBridge(
            project_root=self.project_root, backend_url=self.backend_url, timeout_seconds=180,
        )

    @staticmethod
    def profiles() -> tuple[TurnoverProfile, ...]:
        return tuple(TURNOVER_PROFILES.values())

    def _now(self) -> datetime:
        try:
            value = self._clock(self._business_zone())
        except TypeError:
            value = self._clock()
        if not isinstance(value, datetime):
            value = datetime.now(self._business_zone())
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._business_zone())
        return value

    def _business_zone(self) -> ZoneInfo:
        return _business_tz(self.timezone_name)

    @staticmethod
    def _manifest_ref_map(values: Any) -> dict[str, str]:
        """Parse ``id@version`` references without exposing them to callers."""

        if not isinstance(values, list):
            return {}
        result: dict[str, str] = {}
        for value in values:
            if not isinstance(value, str) or "@" not in value:
                continue
            identifier, version = value.rsplit("@", 1)
            if identifier and version:
                result[identifier] = version
        return result

    def _release_runtime_digests(self, released: Mapping[str, Any]) -> dict[str, str]:
        """Read declared protected files for the KnowledgeBase integrity check."""

        integrity = released.get("integrity")
        declared_files = integrity.get("files") if isinstance(integrity, Mapping) else None
        if not isinstance(declared_files, Mapping):
            return {}
        root = self.project_root.resolve()
        digests: dict[str, str] = {}
        for relative_path in declared_files:
            if not isinstance(relative_path, str) or not relative_path:
                continue
            candidate = (root / Path(relative_path)).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                # The KnowledgeBase validator will reject an unsafe declared
                # path; do not let this helper read outside the project root.
                continue
            try:
                raw = candidate.read_bytes()
            except OSError:
                # Leave missing files for verify_runtime_snapshot_integrity to
                # report as a failed protected-file check.
                continue
            digests[relative_path.replace("\\", "/")] = sha256(raw).hexdigest()
        return digests

    def _assert_contract_released(self) -> None:
        """Require the turnover contract to be approved and production-released.

        ``TurnoverReportRuntime`` talks directly to the Doris HTTP backend, so
        it cannot rely on ``QueryService``'s normal preflight gate.  Keep an
        equivalent check here and run it before SQL compilation or any backend
        call.  The explicit override is retained only for controlled smoke
        tests and is disabled by default.
        """

        if self.allow_unapproved_contracts:
            LOGGER.warning(
                "turnover contract release gate bypassed for explicit pre-release smoke test"
            )
            return

        try:
            if self._knowledge_injected:
                knowledge = self.knowledge
            else:
                snapshot_root, trusted_digest = active_snapshot(self.project_root)
                knowledge = KnowledgeBase(
                    snapshot_root,
                    reload_on_change=True,
                    trusted_release_sha256=trusted_digest,
                    require_trusted_release=trusted_digest is not None,
                )
                self.knowledge = knowledge
            if knowledge is None:
                raise ValueError("knowledge base unavailable")

            metric = knowledge.metric(REPORT_METRIC_ID)
            raw_metric_version = metric.get("version")
            if not isinstance(raw_metric_version, str) or not raw_metric_version.strip():
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="metric_version_invalid",
                )
            metric_version = raw_metric_version.strip()
            metric_ref = f"{REPORT_METRIC_ID}@{metric_version}"
            approval = metric.get("approval")
            approval_state = approval.get("state") if isinstance(approval, Mapping) else None
            if metric.get("status") != "active" or approval_state != "approved":
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="metric_not_active_or_approved",
                )

            released = knowledge.released_manifest()
            if not isinstance(released, Mapping):
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="released_manifest_missing",
                )
            release_approval = released.get("approval")
            release_approval_state = (
                release_approval.get("state")
                if isinstance(release_approval, Mapping)
                else None
            )
            if (
                released.get("status") != "released"
                or release_approval_state != "approved"
                or not released.get("released_at")
            ):
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="release_not_approved",
                )

            contracts = released.get("contracts")
            if not isinstance(contracts, Mapping):
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="release_contracts_invalid",
                )
            released_metrics = contracts.get("metrics")
            if not isinstance(released_metrics, list) or metric_ref not in released_metrics:
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="released_metric_mismatch",
                )

            sources = metric.get("sources")
            if not isinstance(sources, Mapping):
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="metric_sources_invalid",
                )
            source_ids: list[str] = []
            for source_kind in ("required", "conditional"):
                values = sources.get(source_kind, [])
                if not isinstance(values, list):
                    raise TurnoverRuntimeError(
                        "report_contract_not_released",
                        "周转报表合同尚未发布，未生成图片",
                        reason="metric_sources_invalid",
                    )
                if any(
                    not isinstance(source_id, str) or not source_id.strip()
                    for source_id in values
                ):
                    raise TurnoverRuntimeError(
                        "report_contract_not_released",
                        "周转报表合同尚未发布，未生成图片",
                        reason="metric_sources_invalid",
                    )
                source_ids.extend(
                    str(source_id)
                    for source_id in values
                    if isinstance(source_id, str) and source_id.strip()
                )
            source_ids = list(dict.fromkeys(source_ids))
            if not source_ids:
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="metric_sources_invalid",
                )
            _release_id, selected_versions = knowledge.plan_release_context(
                metric_id=REPORT_METRIC_ID,
                metric_version=metric_version,
                source_ids=tuple(source_ids),
            )
            released_sources = self._manifest_ref_map(contracts.get("sources"))
            source_mismatch = [
                source_id
                for source_id in source_ids
                if not selected_versions.get(source_id)
                or released_sources.get(source_id) != selected_versions.get(source_id)
            ]
            if source_mismatch or len(selected_versions) != len(source_ids):
                raise TurnoverRuntimeError(
                    "report_contract_not_released",
                    "周转报表合同尚未发布，未生成图片",
                    reason="released_source_mismatch",
                )

            # Recheck the protected bytes at the execution boundary.  This is
            # meaningful only for a real released manifest; candidates are
            # already rejected above.
            knowledge.verify_runtime_snapshot_integrity(
                {path: sha256((knowledge.project_root / path).read_bytes()).hexdigest() for path in released.get("integrity", {}).get("files", {})}
            )
        except TurnoverRuntimeError:
            raise
        except Exception as exc:
            # Keep paths, contract contents and integrity details out of the
            # browser response.  The type is enough for controlled logs.
            LOGGER.warning("turnover contract gate failed: %s", type(exc).__name__)
            raise TurnoverRuntimeError(
                "report_contract_not_released",
                "周转报表合同尚未发布，未生成图片",
                reason="knowledge_base_unavailable",
            ) from exc

    def _validate_source_quality(
        self,
        rows: list[Mapping[str, Any]],
        report: TurnoverReport,
        *,
        now: datetime,
        profile: TurnoverProfile | None = None,
    ) -> None:
        if not rows:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "没有可核对的报表数据，未生成图片", reason="empty_result"
            )
        # The SQL carries table-level maxima on every row.  Fall back to row
        # maxima for fixture/older query compatibility, but never treat a
        # missing source timestamp as fresh.
        def _quality_value(*names: str) -> Any:
            for candidate in rows:
                value = _row_value(candidate, *names)
                if value not in (None, ""):
                    return value
            return None

        if report.inventory_freshness_mode == "table_update_time":
            stock_max = _quality_value(
                "source_table_max_inventory_update_time",
                "source_table_max_stock_ingestion_update_time",
                "stock_table_max",
                "max_stock_change_time",
            )
        else:
            stock_max = _quality_value(
                "source_table_max_stock_ingestion_update_time",
                "source_table_max_stock_change_time",
                "stock_table_max",
                "max_stock_change_time",
            )
        outbound_max = _quality_value(
            "source_table_max_outbound_ingestion_update_time",
            "source_table_max_ship_at",
            "outbound_table_max",
            "max_ship_at",
        )
        wms_max = _quality_value(
            "source_table_max_wms_ingestion_update_time",
            "source_table_max_wms_updated_date",
            "wms_table_max",
            "max_wms_updated_date",
        )
        if stock_max is None:
            stock_max = max(
                (row.source_max_stock_change_time for row in report.source_rows),
                key=lambda value: str(value or ""),
                default=None,
            )
        if outbound_max is None:
            outbound_max = max(
                (row.source_max_ship_at for row in report.source_rows),
                key=lambda value: str(value or ""),
                default=None,
            )
        checks = [("库存", stock_max, "stock_freshness_missing"),
                  ("出库", outbound_max, "outbound_freshness_missing"),
                  ("仓库主数据", wms_max, "wms_freshness_missing")]
        if profile is not None and profile.require_catalog_freshness:
            checks.append(
                (
                    "SKU主数据",
                    _quality_value("source_table_max_sku_ingestion_update_time"),
                    "sku_freshness_missing",
                )
            )
        threshold = now.astimezone(self._business_zone()) - timedelta(hours=self.freshness_lag_hours)
        for label, value, missing_reason in checks:
            parsed = _parse_source_timestamp(value, self.timezone_name)
            if parsed is None:
                # Missing maxima are not evidence of a zero business value.
                # A source outage and a genuinely empty seven-day window must
                # be distinguishable before an image is published.
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    f"{label}数据新鲜度无法确认，未生成图片",
                    reason=missing_reason,
                )
            if parsed > now.astimezone(self._business_zone()) + timedelta(minutes=5):
                raise TurnoverRuntimeError(
                    "report_quality_blocked", "数据源时间异常，未生成图片", reason=f"{label}_future_timestamp"
                )
            if parsed < threshold:
                raise TurnoverRuntimeError(
                    "report_quality_blocked", f"{label}数据已超过新鲜度阈值，未生成图片", reason=f"{label}_stale"
                )

    @staticmethod
    def _canonical_quality_value(value: Any, timezone_name: str) -> tuple[str, str]:
        """Canonicalise a repeated SQL control value without leaking it."""

        if isinstance(value, bool):
            return ("bool", "1" if value else "0")
        if isinstance(value, (Decimal, int, float)):
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, ValueError):
                return ("invalid", str(value))
            if not parsed.is_finite():
                return ("invalid", str(value))
            return ("number", format(parsed, "f"))
        if isinstance(value, str):
            text = value.strip()
            try:
                parsed_number = Decimal(text)
            except (InvalidOperation, ValueError):
                parsed_number = None
            if parsed_number is not None and parsed_number.is_finite():
                return ("number", format(parsed_number, "f"))
        parsed_timestamp = _parse_source_timestamp(value, timezone_name)
        if parsed_timestamp is not None:
            return ("timestamp", parsed_timestamp.isoformat())
        return ("text", str(value).strip())

    def _quality_controls(
        self,
        rows: list[Mapping[str, Any]],
        report: TurnoverReport,
        profile: TurnoverProfile | None = None,
    ) -> dict[str, Any]:
        """Validate SQL-side counters and independent control totals.

        The query repeats one set of controls on every warehouse row.  Read a
        consistent value once; summing a repeated control would multiply it by
        the number of warehouses and make a seemingly plausible report wrong.
        """

        def global_value(name: str) -> Any:
            present = [row.get(name) for row in rows if row.get(name) not in (None, "")]
            if not present:
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    "报表完整性校验信息缺失，未生成图片",
                    reason=f"{name}_missing",
                )
            canonical = [self._canonical_quality_value(value, self.timezone_name) for value in present]
            if any(kind == "invalid" for kind, _value in canonical) or len(set(canonical)) != 1:
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    "报表完整性校验信息不一致，未生成图片",
                    reason=f"{name}_inconsistent",
                )
            # A cross-joined control must be present on every output row.  A
            # partial value is indistinguishable from a truncated result.
            if len(present) != len(rows):
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    "报表完整性校验信息不完整，未生成图片",
                    reason=f"{name}_partial",
                )
            return present[0]

        def nonnegative_int(name: str) -> int:
            value = global_value(name)
            try:
                decimal = Decimal(str(value).strip())
                if not decimal.is_finite() or decimal != decimal.to_integral_value():
                    raise ValueError
                parsed = int(decimal)
            except (InvalidOperation, TypeError, ValueError):
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    "报表完整性校验信息无效，未生成图片",
                    reason=f"{name}_invalid",
                )
            if parsed < 0:
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    "报表完整性校验信息无效，未生成图片",
                    reason=f"{name}_negative",
                )
            return parsed

        def decimal_value(name: str) -> Decimal:
            value = global_value(name)
            try:
                parsed = Decimal(str(value).strip())
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    "报表完整性校验信息无效，未生成图片",
                    reason=f"{name}_invalid",
                ) from exc
            if not parsed.is_finite() or parsed < 0:
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    "报表完整性校验信息无效，未生成图片",
                    reason=f"{name}_invalid",
                )
            return parsed

        def optional_nonnegative_int(name: str) -> int | None:
            if not any(row.get(name) not in (None, "") for row in rows):
                return None
            return nonnegative_int(name)

        controls = {
            "warehouse_scope_row_count": nonnegative_int("warehouse_scope_row_count"),
            "warehouse_center_row_count": nonnegative_int("warehouse_center_row_count"),
            "warehouse_detail_row_count": nonnegative_int("warehouse_detail_row_count"),
            "warehouse_mapping_conflict_count": nonnegative_int("warehouse_mapping_conflict_count"),
            "invalid_parent_mapping_count": nonnegative_int("invalid_parent_mapping_count"),
            "unresolved_mapping_count": nonnegative_int("unresolved_mapping_count"),
            # A node-10 row remains an independent center even when its
            # source parent fields are stale. Keep the anomaly auditable;
            # only child mapping failures block output.
            "center_parent_anomaly_count": nonnegative_int("center_parent_anomaly_count"),
            "source_table_inventory_row_count": nonnegative_int("source_table_inventory_row_count"),
            "source_table_outbound_header_count": nonnegative_int("source_table_outbound_header_count"),
            "ambiguous_outbound_count": nonnegative_int("ambiguous_outbound_count"),
            "control_inventory_total_ea": decimal_value("control_inventory_total_ea"),
            "control_outbound_total_ea": decimal_value("control_outbound_total_ea"),
            "control_inventory_row_count": nonnegative_int("control_inventory_row_count"),
            "control_outbound_row_count": nonnegative_int("control_outbound_row_count"),
            "control_negative_stock_count": nonnegative_int("control_negative_stock_count"),
            "control_negative_outbound_count": nonnegative_int("control_negative_outbound_count"),
        }
        for optional_name in (
            "center_name_mismatch_count",
            "inventory_missing_warehouse_count",
            "inventory_name_conflict_count",
            "control_duplicate_inventory_key_count",
            "expected_center_count",
            "inventory_base_source_row_count",
        ):
            optional_value = optional_nonnegative_int(optional_name)
            if optional_value is not None:
                controls[optional_name] = optional_value

        if controls.get("inventory_base_source_row_count") == 0 or controls["source_table_inventory_row_count"] == 0:
            raise TurnoverRuntimeError(
                "report_quality_blocked",
                "库存来源没有可核验记录，无法计算周转；未将缺失库存计为零",
                reason="inventory_source_empty",
            )

        if controls["warehouse_mapping_conflict_count"]:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "仓库主数据映射存在冲突，未生成图片", reason="warehouse_mapping_conflict"
            )
        if controls["invalid_parent_mapping_count"]:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "仓库主数据存在非法归属，未生成图片", reason="warehouse_mapping_invalid_parent"
            )
        if controls["unresolved_mapping_count"]:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "存在未解析的仓库归属，未生成图片", reason="warehouse_mapping_unresolved"
            )
        if controls["ambiguous_outbound_count"]:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "出库单归属不唯一，未生成图片", reason="ambiguous_outbound"
            )
        if controls["control_negative_outbound_count"]:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "报表存在未处理的异常数据，未生成图片", reason="negative_control_total"
            )
        if controls["control_negative_stock_count"] and (
            profile is None or profile.negative_inventory_policy == "block"
        ):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "报表存在未处理的异常数据，未生成图片", reason="negative_control_total"
            )
        if controls.get("center_name_mismatch_count", 0):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "中心仓名称与主数据不一致，未生成图片", reason="center_name_mismatch"
            )
        if controls.get("inventory_missing_warehouse_count", 0) and (
            profile is None or profile.missing_inventory_policy == "block"
        ):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "存在没有库存事实的纳入仓库，未生成图片", reason="inventory_missing_warehouse"
            )
        if controls.get("inventory_name_conflict_count", 0):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "库存仓库名称对应多个编码，未生成图片", reason="inventory_name_conflict"
            )
        if controls.get("control_duplicate_inventory_key_count", 0):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "库存复合粒度存在重复，未生成图片", reason="inventory_composite_key_duplicate"
            )

        if controls["warehouse_scope_row_count"] != len(report.source_rows):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "仓库范围与结果行数不一致，未生成图片", reason="warehouse_scope_count_mismatch"
            )
        if controls["warehouse_center_row_count"] != len(report.center_rows):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "中心仓范围与结果不一致，未生成图片", reason="warehouse_center_count_mismatch"
            )
        if controls["warehouse_detail_row_count"] != len(report.detail_rows):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "分仓范围与结果不一致，未生成图片", reason="warehouse_detail_count_mismatch"
            )
        if (
            controls.get("expected_center_count") is not None
            and controls["expected_center_count"] != len(report.center_rows)
        ):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "中心仓数量与固定业务范围不一致，未生成图片", reason="expected_center_count_mismatch"
            )

        def row_decimal(name: str) -> Decimal:
            total = Decimal("0")
            for row in rows:
                value = row.get(name, 0)
                if value in (None, ""):
                    value = 0
                try:
                    parsed = Decimal(str(value).strip())
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise TurnoverRuntimeError(
                        "report_quality_blocked",
                        "报表结果数值无效，未生成图片",
                        reason=f"{name}_invalid",
                    ) from exc
                if not parsed.is_finite() or parsed < 0:
                    raise TurnoverRuntimeError(
                        "report_quality_blocked",
                        "报表结果数值无效，未生成图片",
                        reason=f"{name}_negative",
                    )
                total += parsed
            return total

        def row_count(name: str) -> int:
            total = 0
            for row in rows:
                value = row.get(name, 0)
                if value in (None, ""):
                    value = 0
                try:
                    decimal = Decimal(str(value).strip())
                    if not decimal.is_finite() or decimal != decimal.to_integral_value():
                        raise ValueError
                    parsed = int(decimal)
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise TurnoverRuntimeError(
                        "report_quality_blocked",
                        "报表结果计数无效，未生成图片",
                        reason=f"{name}_invalid",
                    ) from exc
                if parsed < 0:
                    raise TurnoverRuntimeError(
                        "report_quality_blocked",
                        "报表结果计数无效，未生成图片",
                        reason=f"{name}_negative",
                    )
                total += parsed
            return total

        checks = (
            ("inventory", row_decimal("available_ea"), controls["control_inventory_total_ea"]),
            ("outbound", row_decimal("outbound_7d_ea"), controls["control_outbound_total_ea"]),
        )
        for label, actual, expected in checks:
            if abs(actual - expected) > report.tolerance:
                raise TurnoverRuntimeError(
                    "report_quality_blocked",
                    f"{label}总量对账未通过，未生成图片",
                    reason=f"{label}_control_total_mismatch",
                )
        if row_count("inventory_row_count") != controls["control_inventory_row_count"]:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "库存明细计数对账未通过，未生成图片", reason="inventory_row_count_mismatch"
            )
        if row_count("outbound_row_count") != controls["control_outbound_row_count"]:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "出库明细计数对账未通过，未生成图片", reason="outbound_row_count_mismatch"
            )
        if controls["center_parent_anomaly_count"]:
            LOGGER.warning(
                "turnover report contains %s source parent differences; reviewed report hierarchy retained",
                controls["center_parent_anomaly_count"],
            )
        if controls["control_negative_stock_count"]:
            LOGGER.warning(
                "turnover report retained %s negative inventory source facts without clamping",
                controls["control_negative_stock_count"],
            )
        if controls.get("inventory_missing_warehouse_count", 0):
            LOGGER.warning(
                "turnover report treated %s missing inventory warehouse facts as zero under the explicit profile policy",
                controls["inventory_missing_warehouse_count"],
            )
        return controls

    def run(
        self,
        *,
        principal_hash: str,
        conversation_id: str,
        request_id: str,
        end_date_exclusive: Any = None,
        window_days: int = 7,
        profile_id: str = DEFAULT_PROFILE_ID,
        width: int = 2200,
    ) -> dict[str, Any]:
        profile = TURNOVER_PROFILES.get(str(profile_id))
        if type(window_days) is not int or not 1 <= window_days <= 3650:
            raise TurnoverRuntimeError("report_parameters_invalid", "周转统计天数必须是1至3650个完整自然日", reason="window_days")
        if profile is None:
            raise TurnoverRuntimeError(
                "report_parameters_invalid", "报表口径选项无效", reason="unknown_profile"
            )
        # This runtime bypasses QueryService and therefore owns its own
        # production release/approval gate.  It must run before SQL generation
        # and before any request reaches the Doris backend.
        self._assert_contract_released()
        if not self.allow_unapproved_contracts and profile.inventory_basis != "available_plus_transit":
            raise TurnoverRuntimeError("report_contract_not_released", "该库存口径尚未发布，请使用已确认的实时成品库存口径", reason="profile_not_released")
        now = self._now()
        start, end = complete_window(
            end_date_exclusive,
            timezone_name=self.timezone_name,
            now=now,
            window_days=window_days,
        )
        template = None
        if self.knowledge is not None:
            metric = self.knowledge.metric(REPORT_METRIC_ID)
            if (window_days == 14 and profile.operating_scope
                    and metric.get("filters", {}).get("template_profile")):
                template = load_turnover_template(self.knowledge.project_root)
                if self.cargo_owner_id != "HJXH":
                    raise TurnoverRuntimeError("report_parameters_invalid", "该仓库模板仅适用于已确认的货主", reason="template_owner")
        profile_name = "实时可用成品库存 + 在途（9个中心、42个分仓）" if template else profile.display_name
        try:
            if profile.operating_scope:
                sql = build_operating_center_turnover_sql(
                    start,
                    end,
                    cargo_owner_id=self.cargo_owner_id,
                    inventory_basis=profile.inventory_basis,
                    inventory_status=profile.inventory_status,
                    window_days=window_days,
                    warehouse_template=template,
                )
            else:
                # Compatibility path for callers that register a legacy
                # profile in a downstream deployment.
                from .turnover_report import build_turnover_sql

                sql = build_turnover_sql(
                    start,
                    end,
                    cargo_owner_id=self.cargo_owner_id,
                    inventory_basis=profile.inventory_basis,
                    inventory_status=profile.inventory_status,
                    window_days=window_days,
                )
            report_progress("query")
            response = self.query_client.query(
                sql=sql,
                purpose=f"{REPORT_KIND}:{profile.profile_id}",
            )
        except DorisResultError:
            # Keep the transport/backend category intact for receipts and UI.
            raise
        except TurnoverReportError as exc:
            raise TurnoverRuntimeError(
                "report_configuration_error", "报表口径配置无效，未生成图片", reason="sql_contract"
            ) from exc
        except Exception as exc:
            LOGGER.warning("turnover query failed: %s", type(exc).__name__)
            raise TurnoverRuntimeError(
                "report_query_failed", "报表查询未完成，未生成图片", reason="query_failed"
            ) from exc

        # Reports and ordinary queries require the same complete live evidence.
        # Preserve typed failures so the task receipt retains their real cause.
        rows = [dict(row) for row in validate_doris_result(response).rows]
        for row in rows:
            if "__sales_join_conflicts" in row:
                try:
                    conflicts = int(row["__sales_join_conflicts"])
                except (TypeError, ValueError):
                    conflicts = -1
                if conflicts != 0:
                    raise TurnoverRuntimeError("report_quality_blocked", "销售关联存在重复单号或主数据，未生成周转图片", reason="sales_join_conflict")
        # Quality counters are produced inside the governed SQL.  A positive
        # value means the aggregate clipped/ignored facts and therefore cannot
        # be represented as an exact business image.
        def _counter(name: str) -> int:
            total = 0
            for row in rows:
                value = row.get(name, 0)
                try:
                    parsed = int(value or 0)
                except (TypeError, ValueError):
                    return 1
                if parsed < 0:
                    return 1
                total += parsed
            return total

        negative_stock_count = _counter("negative_stock_count")
        if (
            _counter("negative_outbound_count")
            or _counter("ambiguous_outbound_count")
            or _counter("duplicate_inventory_key_count")
            or (negative_stock_count and profile.negative_inventory_policy == "block")
        ):
            raise TurnoverRuntimeError(
                "report_quality_blocked", "报表存在未处理的异常数据，未生成图片", reason="negative_or_ambiguous_facts"
            )
        try:
            report = build_turnover_report(
                rows,
                start,
                end,
                profile_name=profile_name,
                timezone_name=self.timezone_name,
                inventory_freshness_mode=profile.inventory_freshness_mode,
                center_summary_mode=profile.center_summary_mode,
                window_days=window_days,
            )
            self._validate_source_quality(rows, report, now=now, profile=profile)
            controls = self._quality_controls(rows, report, profile=profile)
            center_report = report
            if template:
                required = ("center_outbound_ea", "center_outbound_row_count",
                            "control_center_outbound_total_ea", "control_center_outbound_row_count")
                if any(any(row.get(key) is None for key in required) for row in rows):
                    raise TurnoverReportError("missing seven-day evidence")
                expected = {w.code: w.center_code for w in template.warehouses}
                actual = {r.wms_code: r.belonging_center_code for r in report.source_rows}
                if expected != actual or len(rows) != len(expected):
                    raise TurnoverReportError("report template warehouse scope mismatch")
                center_rows = [dict(row,
                    outbound_7d_ea=row["center_outbound_ea"],
                    outbound_row_count=row["center_outbound_row_count"],
                    control_outbound_total_ea=row["control_center_outbound_total_ea"],
                    control_outbound_row_count=row["control_center_outbound_row_count"],
                ) for row in rows]
                center_report = build_turnover_report(
                    center_rows, end - timedelta(days=template.center_window_days), end,
                    profile_name=profile_name, timezone_name=self.timezone_name,
                    inventory_freshness_mode=profile.inventory_freshness_mode,
                    center_summary_mode=profile.center_summary_mode,
                    window_days=template.center_window_days,
                )
                self._quality_controls(center_rows, center_report, profile=profile)
                if not center_report.reconciliation.passed:
                    raise TurnoverReportError("seven-day reconciliation failed")
        except TurnoverReportError as exc:
            LOGGER.warning("turnover quality validation failed: %s", type(exc).__name__)
            raise TurnoverRuntimeError(
                "report_quality_blocked", "报表质量校验未通过，未生成图片", reason="quality_validation"
            ) from exc
        if not report.reconciliation.passed:
            failed_checks = [
                f"{check.name}:{check.message or 'failed'}"
                for check in report.reconciliation.failures
                if check.severity == "blocking"
            ]
            LOGGER.warning(
                "turnover reconciliation blocked: %s checks=%s",
                report.quality_status,
                "; ".join(failed_checks),
            )
            raise TurnoverRuntimeError(
                "report_quality_blocked", "报表对账未通过，未生成图片", reason="reconciliation_failed"
            )
        if not report.center_rows:
            raise TurnoverRuntimeError(
                "report_quality_blocked", "未找到中心仓汇总，未生成图片", reason="center_layer_empty"
            )

        runtime_quality = (
            "passed_with_warning"
            if negative_stock_count
            or controls.get("inventory_missing_warehouse_count", 0)
            or report.quality_status != "usable"
            else "passed"
        )
        warnings = []
        if negative_stock_count:
            warnings.append("库存含负数源记录，已按净额保留，未归零或删除")
        if controls.get("inventory_missing_warehouse_count", 0):
            warnings.append("部分仓库无库存记录，按已确认口径计零")
        if report.quality_status != "usable":
            warnings.append("部分仓库无动销，周转显示为无动销")

        try:
            report_progress("rendering")
            image = render_turnover_image(
                report,
                width=width,
                detail_columns=3,
                reference_style=True,
                quality_status=runtime_quality,
                center_report=center_report if template else None,
                template=template,
            )
            buffer = BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            content = buffer.getvalue()
            artifact = self.artifact_store.register_bytes(
                content,
                principal_hash=principal_hash,
                conversation_id=conversation_id,
                request_id=request_id,
                kind=REPORT_KIND,
                title="中心仓与分仓周转",
                period=(
                    f"中心7天：{center_report.window_start:%Y-%m-%d} 至 {end:%Y-%m-%d}；"
                    f"分仓14天：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}（均不含结束日，Asia/Shanghai）"
                    if template else
                    f"{start:%Y-%m-%d} 至 {end:%Y-%m-%d}（Asia/Shanghai，{window_days}个完整自然日）"
                ),
            )
        except (TurnoverReportError, OSError, ValueError) as exc:
            LOGGER.warning("turnover image rendering failed: %s", type(exc).__name__)
            raise TurnoverRuntimeError(
                "report_render_failed", "报表图片生成失败，未返回半成品", reason="render_failed"
            ) from exc

        center_business_table = {
            "format": "table", "table_id": "center_warehouse_turnover_7_14" if template else "center_warehouse_turnover",
            "title": f"经营中心{center_report.window_days}日周转", "caption": f"{center_report.window_start:%Y-%m-%d} 至 {end:%Y-%m-%d}（不含结束日） · 各中心含模板分仓 · 可用成品库存加在途 · EA，EA/日，天" + ("。" + "；".join(warnings) if warnings else ""),
            "empty_label": "当前没有可展示的经营中心汇总",
            "columns": [{"key": key} for key in ("warehouse", "current_stock", "daily_sales", "turnover_text")],
            "rows": [
                {"warehouse": group.center.warehouse_name,
                 "current_stock": str(group.center.available_ea),
                  "daily_sales": format(group.center.average_daily_outbound(center_report.window_days), ".2f"),
                  "turnover_text": format(group.center.turnover_days_for(center_report.window_days), ".2f") if group.center.turnover_days_for(center_report.window_days) is not None else "无动销"}
                for group in center_report.groups
            ], "summary": [],
        }
        report_tables = None
        if template:
            report_tables = [
                center_business_table,
                {
                    "format": "table",
                    "table_id": "center_warehouse_turnover_14_detail",
                    "title": "各分仓14日周转",
                    "caption": f"{report.window_start:%Y-%m-%d} 至 {end:%Y-%m-%d}（不含结束日） · 可用成品库存加在途 · EA，EA/日，天",
                    "empty_label": "当前没有可展示的分仓周转明细",
                    "columns": [{"key": key} for key in ("belonging_center", "warehouse", "current_stock", "daily_sales", "turnover_text")],
                    "rows": [
                        {
                            "belonging_center": group.center.warehouse_name,
                            "warehouse": row.warehouse_name,
                            "current_stock": str(row.available_ea),
                            "daily_sales": format(row.average_daily_outbound(report.window_days), ".2f"),
                            "turnover_text": format(row.turnover_days_for(report.window_days), ".2f") if row.turnover_days_for(report.window_days) is not None else "无动销",
                        }
                        for group in report.groups
                        for row in group.details
                    ],
                    "summary": [],
                },
            ]
        return {
            "artifact": artifact,
            "business_table": center_business_table,
            **({"business_tables": report_tables} if report_tables is not None else {}),
            "report_kind": REPORT_KIND,
            "profile_id": profile.profile_id,
            "period": artifact["period"],
            "quality": runtime_quality,
        }


__all__ = [
    "DEFAULT_PROFILE_ID",
    "REPORT_METRIC_ID",
    "REPORT_KIND",
    "ALLOW_UNAPPROVED_ENV",
    "TURNOVER_PROFILES",
    "TurnoverProfile",
    "TurnoverReportRuntime",
    "TurnoverRuntimeError",
    "complete_window",
]
