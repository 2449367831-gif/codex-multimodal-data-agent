"""Private projection and reconciliation contracts for multi-step model queries.

Declarations never supply business values or change units. The SQL backend still
owns execution policy; these checks bind its returned projection to the plan.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import json
import re

import sqlglot

from .public_table_text import business_column_label, business_result_title
from .query_outcomes import DorisResultError
from .query_outcomes import VALIDATION_FAILURES

MAX_ANSWER_TABLES = 4
MAX_ANSWER_ROWS = 100_000
MAX_ANSWER_BYTES = 32 * 1024 * 1024
RESULT_ID = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
BASE_MEASURE_UNITS = frozenset({"EA", "EA/日", "EA/箱", "件", "箱", "天", "个", "单", "吨", "台", "个月",
                               "个完整自然日", "元", "万元", "%", "千克", "克", "小时", "分钟"})
RATIO_NUMERATOR_UNITS = ("EA", "件", "箱", "吨", "台", "千克", "克", "元", "万元", "单", "个")
RATIO_DENOMINATOR_UNITS = ("单", "个", "日", "天", "周", "月", "小时", "分钟", "EA", "件", "箱", "吨", "千克", "克")
MEASURE_UNITS = BASE_MEASURE_UNITS | frozenset(
    f"{numerator}/{denominator}"
    for numerator in RATIO_NUMERATOR_UNITS for denominator in RATIO_DENOMINATOR_UNITS
    if numerator != denominator
)
IDENTITY_LABEL = re.compile(r"(?:编码|名称|单号|源单位代码)$|^(?:SKU|客户|仓库|货主)$", re.IGNORECASE)

# Only these fixed diagnostics may enter a repair prompt. Never forward parser
# exceptions, SQL fragments, model labels or source values as an error message.
PLAN_CONTRACT_ERRORS = {
    "invalid business result identity": "result_identity",
    "invalid reconciliation operand": "check_operand",
    "invalid result column declarations": "column_list",
    "invalid result column declaration": "column_shape",
    "invalid result column identity": "column_identity",
    "invalid result column kind": "column_kind",
    "identity column cannot be numeric": "identity_kind",
    "invalid result measure unit": "measure_unit",
    "invalid query kind": "query_kind",
    "query kind disagrees with sources": "source_kind",
    "only business queries can declare a result identity": "result_kind",
    "business query cannot act as a validation": "result_kind",
    "result columns disagree with query projection": "projection",
    "non-business query cannot declare a display table": "result_kind",
    "validation query requires bounded checks": "check_list",
    "validation requires unambiguous projected columns": "check_projection",
    "invalid validation check": "check_shape",
    "checks require a validation query": "result_kind",
    "query plan must contain a query": "sql_shape",
    "query plan exceeds SQL length limit": "plan_size",
    "query plan purpose must not be empty": "purpose",
    "query plan is not valid Doris syntax": "sql_syntax",
    "query plan must contain one statement": "sql_shape",
    "query plan must be SELECT or WITH ... SELECT": "sql_shape",
    "query plan must be read-only": "sql_shape",
}
PLAN_CONTRACT_HINTS = {
    "result_identity": "交付表必须同时给出合法且唯一的result_id和简洁中文result_title。",
    "check_operand": "对账阈值必须为有限数字，不能为布尔值、空值、表达式或非有限数。",
    "column_list": "business计划必须提供包含一至三十二列的result_columns数组。",
    "column_shape": "每列只允许key、label、kind、unit；必须包含key和label。",
    "column_identity": "key和label必须非空且分别唯一；label只用简短中文业务名称，不含下划线、计算符号、技术信息。例如派生量命名为两倍周转天数，不在表头写乘法表达式。",
    "column_kind": "kind只能为number或text。",
    "identity_kind": "编码、名称和单号必须用text，保留前导零，不可声明number。",
    "measure_unit": "unit只用于number列，且必须采用支持列表中的已确认单位；不能虚构单位或以声明替代换算。",
    "query_kind": "kind只能为metadata、business或validation。",
    "source_kind": "仅无业务来源的结构查询使用metadata；业务源即使同时引用元数据，也不能声明metadata。",
    "result_kind": "业务交付列和result_id只用于business；checks只用于validation，不可混用。",
    "projection": "result_columns的key必须与最外层SELECT的每个唯一输出别名逐一对应；禁止星号投影，不能使用内部CTE的列替代最终输出列。",
    "check_list": "validation计划需要包含一至十六条检查的checks数组。",
    "check_projection": "对账SELECT需要显式且唯一的输出别名，不能使用星号。",
    "check_shape": "每条检查必须含column、operator、value，可选rule；column为该查询输出别名，operator为eq/ge/le，value为有限数字，rule仅用协议支持且当前指标要求的校验规则。",
    "sql_shape": "sql必须是一条非空的只读SELECT或WITH SELECT，不能包含多个语句或写操作。",
    "sql_syntax": "按Doris语法修复查询；解析尚未通过，此计划没有执行。",
    "purpose": "purpose必须是非空的简短字符串。",
    "plan_size": "保持SQL和说明的长度上限；通过合理计划拆分处理，不得删减业务需求。",
    "json_shape": "返回完整JSON对象，字段类型必须符合计划协议。",
    "batch_shape": "批次只能包含outcome和plans，plans包含二至四条完整计划。",
    "batch_duplicate": "本批查询、交付标识和标题不能重复；保留不同交付项及全部需求。",
    "batch_completion": "只有同时包含具名business交付表和至少一条validation对账计划的完整批次，才可声明completion；completion.result_ids必须逐一覆盖本批全部具名business表，不能遗漏空表。",
    "unspecified": "计划声明未通过校验，按完整协议逐项检查，不要重复原格式。",
}


def result_identity(envelope):
    result_id = envelope.get("result_id")
    title = business_result_title(envelope.get("result_title"))
    if not isinstance(result_id, str) or not RESULT_ID.fullmatch(result_id) or title is None:
        raise ValueError("invalid business result identity")
    return {"result_id": result_id, "result_title": title}


class BusinessResultSet:
    """Request-local, bounded results. Named deliverables are never overwritten."""

    def __init__(self):
        self._results = {}
        self._bytes = 0
        self._rows = 0

    def check_plan(self, plan):
        if "result_id" not in plan:
            return
        identity = result_identity(plan)
        if (identity["result_id"] in self._results
                or any(r["result_title"] == identity["result_title"] for r in self._results.values())):
            raise DorisResultError("execution_invalid")
        if len(self._results) >= MAX_ANSWER_TABLES:
            raise DorisResultError("result_truncated")

    def add(self, plan, result):
        self.check_plan(plan)
        if "result_id" not in plan:
            return
        value = {**result, **result_identity(plan)}
        size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        if self._bytes + size > MAX_ANSWER_BYTES or self._rows + len(result["rows"]) > MAX_ANSWER_ROWS:
            raise DorisResultError("result_truncated")
        self._results[value["result_id"]] = value
        self._bytes += size
        self._rows += len(result["rows"])

    def manifest(self):
        return [{"result_id": key, "title": value["result_title"], "row_count": len(value["rows"])}
                for key, value in self._results.items()]

    def select(self, final):
        if not isinstance(final, Mapping) or final.get("outcome") != "completed":
            return None
        ids = final.get("result_ids")
        if not self._results and ids is None:
            return None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ANSWER_TABLES
                or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids)
                or set(ids) != set(self._results)):
            raise ValueError("incomplete business result manifest")
        return [self._results[i] for i in ids]


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid reconciliation operand")
    if len(str(value)) > 128:
        raise ValueError("invalid reconciliation operand")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid reconciliation operand") from None
    if not number.is_finite():
        raise ValueError("invalid reconciliation operand")
    return number


def result_columns(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("invalid result column declarations")
    columns, keys, labels = [], set(), set()
    for column in value:
        if (not isinstance(column, Mapping) or not {"key", "label"} <= set(column)
                or set(column) - {"key", "label", "kind", "unit"}):
            raise ValueError("invalid result column declaration")
        key, label = column["key"], business_column_label(column["label"])
        if (not isinstance(key, str) or not key.strip() or key != key.strip()
                or len(key) > 160 or key.casefold() in keys or label is None or label in labels):
            raise ValueError("invalid result column identity")
        keys.add(key.casefold())
        labels.add(label)
        descriptor = {"key": key, "label": label}
        if "kind" in column:
            kind = column["kind"]
            if not isinstance(kind, str) or kind not in {"number", "text"}:
                raise ValueError("invalid result column kind")
            if kind == "number" and IDENTITY_LABEL.search(label):
                raise ValueError("identity column cannot be numeric")
            descriptor["kind"] = kind
        if "unit" in column:
            unit = column["unit"]
            if (descriptor.get("kind") != "number" or not isinstance(unit, str)
                    or unit not in MEASURE_UNITS):
                raise ValueError("invalid result measure unit")
            descriptor["unit"] = unit
        columns.append(descriptor)
    return columns


def private_plan_options(envelope, *, metadata_query):
    """Legacy plans remain readable; explicit plans use the strict new shape."""
    supplied = {key for key in ("kind", "result_columns", "checks", "result_id", "result_title") if key in envelope}
    if not supplied:
        return {}
    kind = envelope.get("kind", "metadata" if metadata_query else "business")
    if not isinstance(kind, str) or kind not in {"metadata", "business", "validation"}:
        raise ValueError("invalid query kind")
    if (kind == "metadata" and not metadata_query) or (kind == "business" and metadata_query):
        raise ValueError("query kind disagrees with sources")
    options = {"kind": kind}
    if supplied & {"result_id", "result_title"}:
        if kind != "business":
            raise ValueError("only business queries can declare a result identity")
        options.update(result_identity(envelope))
    if kind == "business":
        if "checks" in supplied:
            raise ValueError("business query cannot act as a validation")
        # The model may return a minimal executable business plan. In that
        # mode the host validates the live Doris projection and derives the
        # neutral display shape later; requiring labels and units here made
        # gateway formatting failures look like model/query failures.
        if "result_columns" not in supplied:
            if supplied & {"result_id", "result_title"}:
                raise ValueError("business result identity requires result columns")
            options["kind"] = kind
            return options
        columns = result_columns(envelope.get("result_columns"))
        names = sqlglot.parse_one(envelope["sql"], read="doris").named_selects
        if (not names or "*" in names or len(names) != len(columns)
                or len(set(names)) != len(names) or set(names) != {c["key"] for c in columns}):
            raise ValueError("result columns disagree with query projection")
        options["result_columns"] = columns
    elif "result_columns" in supplied:
        raise ValueError("non-business query cannot declare a display table")
    if kind == "validation":
        checks = envelope.get("checks")
        if not isinstance(checks, list) or not 1 <= len(checks) <= 16:
            raise ValueError("validation query requires bounded checks")
        names = sqlglot.parse_one(envelope["sql"], read="doris").named_selects
        if not names or "*" in names or len(set(names)) != len(names):
            raise ValueError("validation requires unambiguous projected columns")
        checked = []
        for check in checks:
            if (not isinstance(check, Mapping) or not {"column", "operator", "value"} <= set(check)
                    or set(check) - {"column", "operator", "value", "rule"}
                    or not isinstance(check["column"], str) or check["column"] not in names
                    or check["operator"] not in {"eq", "ge", "le"}):
                raise ValueError("invalid validation check")
            if "rule" in check and (not isinstance(check["rule"], str) or check["rule"] not in VALIDATION_FAILURES):
                raise ValueError("invalid validation check")
            _number(check["value"])
            checked.append(dict(check))
        options["checks"] = checked
    elif "checks" in supplied:
        raise ValueError("checks require a validation query")
    return options


def validate_business_projection(execution, declarations):
    """Require the declared columns to exist; never rename by ordinal position."""
    descriptors = result_columns(declarations)
    expected = {column["key"] for column in descriptors}
    columns = execution.get("columns")
    rows = execution.get("rows")
    if (not isinstance(columns, list) or any(not isinstance(c, str) for c in columns)
            or len(columns) != len(expected) or set(columns) != expected
            or not isinstance(rows, list)
            or any(not isinstance(row, Mapping) or set(row) != expected for row in rows)):
        raise DorisResultError("execution_invalid", diagnostic="result_projection_invalid")
    try:
        for descriptor in descriptors:
            if descriptor.get("kind") == "number":
                for row in rows:
                    if row[descriptor["key"]] is not None:
                        _number(row[descriptor["key"]])
    except (KeyError, ValueError, TypeError):
        raise DorisResultError("execution_invalid", diagnostic="result_projection_invalid") from None


def validate_reconciliation(rows, checks):
    """Every live validation row must satisfy every declared scalar check."""
    if not rows:
        raise DorisResultError("reconciliation_failed", diagnostic="reconciliation_failed")
    try:
        for row in rows:
            for check in checks:
                actual, expected = _number(row[check["column"]]), _number(check["value"])
                matches = {"eq": actual == expected, "ge": actual >= expected, "le": actual <= expected}
                if not matches[check["operator"]]:
                    raise DorisResultError("reconciliation_failed", diagnostic="reconciliation_failed",
                                           validation_rule=check.get("rule"))
    except (ValueError, KeyError, TypeError):
        raise DorisResultError("reconciliation_failed", diagnostic="reconciliation_failed") from None
