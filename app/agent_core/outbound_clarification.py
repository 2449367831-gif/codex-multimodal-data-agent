"""One-shot decisions for an otherwise unqualified outbound quantity request."""

import re

from ..query_scope import ScopeResolver
from .sales_aggregation import SalesAggregation
from .semantic_types import SemanticRequest


PROMPTS = {
    "outbound_business": "请选择出库业务。",
    "outbound_measure": "请选择统计项；实发、分配、请求数量与单数不能互相代替。",
    "outbound_time_axis": "昨天按哪个业务时间统计？",
}
OPTIONS = {
    "outbound_business": ("outbound_sales", "outbound_transfer", "outbound_adjustment", "outbound_other", "outbound_all"),
    "outbound_measure": ("outbound_actual_qty", "outbound_allocated_qty", "outbound_requested_qty", "outbound_document_count", "outbound_order_count"),
    "outbound_time_axis": ("outbound_shipped_at", "outbound_order_created_at", "outbound_reviewed_at"),
}
AVAILABLE = {
    "outbound_sales": True,
    "outbound_transfer": True,
    "outbound_all": True,
    "outbound_actual_qty": True,
    "outbound_document_count": True,
    "outbound_shipped_at": True,
}
LABELS = {
    "outbound_sales": "销售出库",
    "outbound_transfer": "调拨出库（按完成状态和实际发出数量）",
    "outbound_adjustment": "调整出库（暂未接入）",
    "outbound_other": "其他出库（暂未接入）",
    "outbound_all": "所有出库（销售与调拨）",
    "outbound_actual_qty": "实发数量",
    "outbound_allocated_qty": "分配数量（单位待确认）",
    "outbound_requested_qty": "请求数量（单位待确认）",
    "outbound_document_count": "出库单数",
    "outbound_order_count": "销售订单数（暂未接入）",
    "outbound_shipped_at": "实际出库时间",
    "outbound_order_created_at": "订单创建时间（暂未接入）",
    "outbound_reviewed_at": "审核时间（暂未接入）",
}


def _outbound_scope_and_grain(question, *, resolver=None):
    if not isinstance(question, str):
        return None
    text = re.sub(r"\s+", "", question).rstrip("。？！?!")
    # Match the entire explanatory wrapper: stripping arbitrary prose could
    # silently erase a warehouse, customer or exclusion outside the quotation.
    wrapper = re.fullmatch(
        r'业务只说[“「"\'](?P<source>[^”」"\']+)[”」"\']，但未说明是'
        r'销售出库、所有出库、实发数量、分配数量还是订单量。'
        r'请列出需要确认的口径，并说明默认不能猜哪个字段', text,
    )
    if wrapper:
        text = wrapper['source']
    if not re.search(r"昨天|昨日", text) or not re.search(r"出库(?:量|数量)", text):
        return None
    # Parse explicit grouping separately, leaving scope identifiers untouched.
    # A full remaining-shape match prevents dropping ranking, units or exclusions.
    dimensions = set()
    group_keys = {"货主": "cargo_owner", "仓库": "warehouse", "sku": "sku"}
    grouped = re.search(r"[，,]?按(货主|仓库|sku)([、,，和及](?:货主|仓库|sku))*(?:汇总|统计)?$", text, re.I)
    if grouped:
        dimensions.update(group_keys[token.lower()] for token in re.findall(r"货主|仓库|sku", grouped[0], re.I))
        text = text[:grouped.start()]
    scope, shape, unsupported = (resolver or ScopeResolver()).resolve(text)
    if unsupported:
        return None
    if ((scope.owner != "ALL" and re.search(r"(?:全部|所有)货主", text))
        or (scope.warehouse_codes and re.search(r"(?:全部|所有)仓库", text))
        or (scope.sku_codes and re.search(r"(?:全部|所有)sku", text, re.I))):
        return None
    for token, key in (("各货主", "cargo_owner"), ("各仓库", "warehouse"), ("各sku", "sku")):
        if token in shape:
            dimensions.add(key)
            shape = shape.replace(token, "")
    shape = re.sub(r"(?:全部|所有)(?:仓库|sku)", "", shape)
    if not re.fullmatch(r"(?:请)?(?:查询|统计|查下|看下)?(?:昨天|昨日)(?:的)?(?:成品)?出库(?:量|数量)", shape):
        return None
    return scope, SalesAggregation(tuple(key for key in group_keys.values() if key in dimensions))


def unresolved_outbound_source(question, *, resolver=None):
    return question if _outbound_scope_and_grain(question, resolver=resolver) is not None else None


def outbound_clarification(question):
    if unresolved_outbound_source(question) is None:
        return None
    return {
        "outcome": "clarification_required",
        "questions": [
            {"id": key, "prompt": prompt, "selection": "single",
             "options": [{"id": option, "label": LABELS[option],
                          "available": AVAILABLE.get(option, False)} for option in OPTIONS[key]]}
            for key, prompt in PROMPTS.items()
        ],
    }


def selected_outbound_request(source, selected, *, interval, resolver=None):
    if not isinstance(selected, dict):
        return None
    if set(selected) != set(OPTIONS):
        return None
    business = selected.get("outbound_business")
    measure = selected.get("outbound_measure")
    time_axis = selected.get("outbound_time_axis")
    if time_axis != "outbound_shipped_at" or measure not in {"outbound_actual_qty", "outbound_document_count"}:
        return None
    parsed = _outbound_scope_and_grain(source, resolver=resolver)
    if parsed is None:
        return None
    scope, aggregation = parsed
    if business == "outbound_transfer":
        if measure != "outbound_actual_qty":
            return None
        return SemanticRequest(
            route="transfer_outbound", start=interval[0], end=interval[1],
            scope=scope, show_warehouses="warehouse" in aggregation.dimensions,
        )
    if business == "outbound_all":
        return SemanticRequest(
            route="sales_aggregate", start=interval[0], end=interval[1],
            scope=scope, sales_aggregation=aggregation,
            all_outbound_types=True,
            show_warehouses="warehouse" in aggregation.dimensions,
        )
    if business != "outbound_sales":
        return None
    return SemanticRequest(route="sales_aggregate", start=interval[0], end=interval[1],
                           scope=scope, sales_aggregation=aggregation,
                           show_warehouses="warehouse" in aggregation.dimensions)


def selected_outbound_block_reason(source, selections):
    from .semantic_types import _normalize_selected_answers
    selected = _normalize_selected_answers(selections)
    if unresolved_outbound_source(source) is None or not selected or set(selected) != set(OPTIONS):
        return None
    if any(selected[key] not in options for key, options in OPTIONS.items()):
        return None
    reasons = []
    if selected['outbound_business'] not in {'outbound_sales', 'outbound_transfer', 'outbound_all'}:
        reasons.append('所选出库业务尚未接入已核验的查询')
    measure = selected['outbound_measure']
    if measure in {'outbound_allocated_qty', 'outbound_requested_qty'}:
        reasons.append('所选数量的单位口径尚未确认，不能用实发数量代替')
    elif measure not in {'outbound_actual_qty', 'outbound_document_count'}:
        reasons.append('所选单数统计尚未接入此查询组合，不能用数量代替')
    if selected['outbound_business'] == 'outbound_transfer' and measure == 'outbound_document_count':
        reasons.append('调拨出库单数统计尚未接入此查询组合')
    if selected['outbound_time_axis'] != 'outbound_shipped_at':
        reasons.append('所选业务时间尚未接入此查询组合，不能用实际出库时间代替')
    if not reasons:
        return None
    return '已保留本次选择。' + '；'.join(reasons) + '。本次未执行查询，无需重复选择。'
