"""Validated SKU specifications and exact post-aggregation box rounding."""

from dataclasses import dataclass
from decimal import Decimal

from sqlglot import exp


@dataclass(frozen=True, slots=True)
class BoxSpec:
    value: Decimal | None
    state: str


def box_control_sql(codes):
    if not codes or len(codes) > 200 or len(set(codes)) != len(codes):
        raise ValueError("invalid SKU batch")
    literals = ", ".join(exp.Literal.string(code).sql(dialect="doris") for code in codes)
    return f"""SELECT TRIM(sku_code) AS sku_code, COUNT(*) AS record_count,
  COUNT(DISTINCT CASE WHEN ea_convert_quantity > 0 THEN ea_convert_quantity END) AS positive_variants,
  SUM(CASE WHEN ea_convert_quantity IS NULL OR ea_convert_quantity <= 0 THEN 1 ELSE 0 END) AS invalid_count,
  MIN(CASE WHEN ea_convert_quantity > 0 THEN ea_convert_quantity END) AS box_spec
FROM ywzt_ods.master_sku_unit
WHERE TRIM(unit) = '40' AND TRIM(sku_code) IN ({literals})
GROUP BY TRIM(sku_code) ORDER BY TRIM(sku_code) LIMIT 201"""


def _count(value):
    if isinstance(value, bool):
        raise ValueError("invalid control count")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("invalid control count")
    return int(number)


def resolve_box_specs(codes, rows):
    if len(set(codes)) != len(codes):
        raise ValueError("duplicate requested keys")
    result = {code: BoxSpec(None, "missing") for code in codes}
    seen = set()
    for row in rows:
        code = row.get("sku_code")
        if code not in result or code in seen:
            raise ValueError("unexpected or duplicate unit key")
        seen.add(code)
        records, variants, invalid = (_count(row.get(k)) for k in ("record_count", "positive_variants", "invalid_count"))
        value = Decimal(str(row["box_spec"])) if row.get("box_spec") is not None else None
        if records < 1 or invalid > records or variants > records - invalid:
            raise ValueError("inconsistent unit controls")
        if (variants == 0) != (value is None) or (variants == 0) != (records == invalid):
            raise ValueError("missing unit control")
        if value is not None and (not value.is_finite() or value <= 0 or value != value.to_integral_value()):
            raise ValueError("invalid unit specification")
        state = "conflict" if variants > 1 else "invalid" if invalid else "valid"
        result[code] = BoxSpec(value if state == "valid" else None, state)
    return result


def rounded_boxes(quantity: Decimal, spec: Decimal) -> str:
    if not quantity.is_finite() or quantity < 0 or not spec.is_finite() or spec <= 0:
        raise ValueError("invalid box conversion")
    # Integer ratios preserve exact half boundaries even beyond Decimal context precision.
    numerator, denominator = quantity.as_integer_ratio()
    spec_numerator, spec_denominator = spec.as_integer_ratio()
    quotient, remainder = divmod(numerator * spec_denominator, denominator * spec_numerator)
    return str(quotient + (2 * remainder >= denominator * spec_numerator))


def convert_box_rows(rows, specs):
    result = []
    for row in rows:
        spec = specs[row["sku_code"]]
        result.append({**row, "box_spec":format(spec.value, "f") if spec.value is not None else None,
                       "box_quantity":rounded_boxes(Decimal(row["quantity"]), spec.value) if spec.value is not None else None,
                       "box_state":spec.state})
    return result
