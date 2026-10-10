from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .errors import ParameterValidationError


ALL = "ALL"


def _parse_datetime(name: str, value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ParameterValidationError(
                f"Parameter {name} must be an ISO datetime",
                details={"parameter": name, "value": value},
            ) from exc
    else:
        raise ParameterValidationError(
            f"Parameter {name} must be a datetime string",
            details={"parameter": name},
        )
    if parsed.tzinfo is not None:
        raise ParameterValidationError(
            f"Parameter {name} must use Doris business-local time without an offset",
            details={"parameter": name},
        )
    return parsed


def _parse_date(name: str, value: Any) -> date:
    if isinstance(value, datetime):
        raise ParameterValidationError(
            f"Parameter {name} must be a date, not a datetime",
            details={"parameter": name},
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ParameterValidationError(
                f"Parameter {name} must use YYYY-MM-DD",
                details={"parameter": name, "value": value},
            ) from exc
    raise ParameterValidationError(
        f"Parameter {name} must be a date string",
        details={"parameter": name},
    )


def _parse_integer(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ParameterValidationError(f"Parameter {name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ParameterValidationError(f"Parameter {name} must be an integer") from exc
    raise ParameterValidationError(f"Parameter {name} must be an integer")


def _normalize_scope(name: str, value: Any) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return ALL if stripped.upper() == ALL else stripped
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return [item.strip() for item in value]
    raise ParameterValidationError(
        f"Parameter {name} must be ALL, a non-empty string, or a non-empty string list",
        details={"parameter": name},
    )


class ContractParameterValidator:
    def validate(self, metric: dict[str, Any], supplied: dict[str, Any] | None) -> dict[str, Any]:
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, dict):
            raise ParameterValidationError("parameters must be a JSON object")

        specifications = metric.get("parameters", {})
        if not isinstance(specifications, dict):
            raise ParameterValidationError(f"Metric {metric['id']} has an invalid parameter contract")

        unknown = sorted(set(supplied) - set(specifications))
        if unknown:
            raise ParameterValidationError(
                "Unknown parameters are not permitted",
                details={"unknown": unknown, "allowed": sorted(specifications)},
            )

        normalized: dict[str, Any] = {}
        missing: list[str] = []
        for name, raw_specification in specifications.items():
            specification = raw_specification if isinstance(raw_specification, dict) else {}
            if name in supplied:
                value = supplied[name]
            elif "default" in specification:
                value = specification["default"]
            elif specification.get("required"):
                missing.append(name)
                continue
            else:
                value = None

            if value is None and specification.get("required"):
                missing.append(name)
                continue
            if value is None:
                normalized[name] = None
                continue

            declared_type = specification.get("type")
            if declared_type == "datetime":
                normalized[name] = _parse_datetime(name, value)
            elif declared_type == "date":
                normalized[name] = _parse_date(name, value)
            elif declared_type == "integer":
                integer_value = _parse_integer(name, value)
                if name == "occupation_lookback_days" and not 1 <= integer_value <= 3650:
                    raise ParameterValidationError(
                        "occupation_lookback_days must be between 1 and 3650",
                        details={"parameter": name},
                    )
                normalized[name] = integer_value
            elif name == "output_grain":
                if not isinstance(value, str):
                    raise ParameterValidationError("output_grain must be a string")
                choices = specification.get("examples", [])
                if choices and value not in choices:
                    raise ParameterValidationError(
                        "output_grain is not declared by the metric contract",
                        details={"value": value, "allowed": choices},
                    )
                normalized[name] = value
            elif name.endswith("_scope") or name == "cargo_owner":
                normalized[name] = _normalize_scope(name, value)
            else:
                normalized[name] = value

        if missing:
            raise ParameterValidationError(
                "Required parameters are missing",
                details={"missing": sorted(missing)},
            )

        self._validate_time_order(metric["id"], normalized)
        return normalized

    @staticmethod
    def _validate_time_order(metric_id: str, parameters: dict[str, Any]) -> None:
        time_pairs = (
            ("start_inclusive", "end_exclusive"),
            ("month_start", "sales_end_exclusive"),
        )
        for start_name, end_name in time_pairs:
            start = parameters.get(start_name)
            end = parameters.get(end_name)
            if isinstance(start, datetime) and isinstance(end, datetime) and start >= end:
                raise ParameterValidationError(
                    f"{start_name} must be earlier than {end_name}",
                    details={"metric_id": metric_id},
                )

