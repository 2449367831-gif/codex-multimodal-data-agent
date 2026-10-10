"""Reviewed grouping vocabulary shared by sales planning and analysis."""

import re


DIMENSION_NAMES = {
    "日期": ("按日期", "ship_date"), "货主": ("按货主", "cargo_owner"),
    "仓库": ("按仓库", "warehouse"), "仓": ("按仓库", "warehouse"),
    "出库状态": ("按出库状态", "shipment_status"), "状态": ("按出库状态", "shipment_status"),
    "出库类型": ("按出库类型", "ref_order_type"), "类型": ("按出库类型", "ref_order_type"),
    "sku": ("按SKU", "sku"), "履约类型": ("按2C和2B", "fulfillment_type"),
    "客户渠道": ("按渠道", "customer_channel"), "渠道": ("按渠道", "customer_channel"),
}
# SKU counts and identifiers are projections, not grouping dimensions.
DIMENSION_TOKEN = "(?:" + "|".join(
    re.escape(name) + (r"(?!数|数量|编码|名称)" if name == "sku" else "")
    for name in sorted(DIMENSION_NAMES, key=len, reverse=True)
) + ")"
GROUPS = re.compile(
    rf"按\s*(?P<names>{DIMENSION_TOKEN}(?:\s*[和与及、,，+]\s*{DIMENSION_TOKEN})*)(?![A-Za-z0-9_])", re.I
)


def dimension_names(match):
    return [name.strip().casefold() for name in re.split(r"[和与及、,，+]", match["names"])]


def requested_dimensions(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        DIMENSION_NAMES[name][1] for match in GROUPS.finditer(question) for name in dimension_names(match)
    ))
