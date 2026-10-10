"""Opt-in box query routing; unknown clauses remain unconsumed."""

import re
from dataclasses import replace

import yaml

from .sales_aggregation import SalesAggregation
from .semantic_types import SemanticRequest
from ..sku_references import extract_sku_references, restore_sku_references


def outbound_box_request(question, *, interval, resolver, contract_root):
    sku_codes, semantic_question = extract_sku_references(question)
    text = re.sub(r"\s+", "", semantic_question).casefold()
    if not all(word in text for word in ("成品", "出库", "箱规", "四舍五入")):
        return None
    if not re.search(r"不保留小数|保留0位小数|取整数", text) or "汇总" not in text or not interval:
        return None
    if not re.search(r"jyck",text) or any(token in text for token in ("2c","2b","履约","每行","逐行","向上","向下","例外")):
        return None
    if re.search(r"hhck|bfck|qtck|so\d+", text):
        return None
    contract = yaml.safe_load((contract_root / "metrics/sales.outbound_boxes.yaml").read_text(encoding="utf-8")) if (contract_root / "metrics/sales.outbound_boxes.yaml").exists() else {}
    if contract.get("status") != "active" or contract.get("approval",{}).get("state") != "approved":
        return None
    clauses = ("箱数按汇总后的数量除以箱规", "按汇总后的数量除以箱规", "四舍五入且不保留小数", "四舍五入不保留小数", "四舍五入且保留0位小数", "四舍五入取整数",
               "原始实发数量", "实发箱数", "按日期、仓库、SKU、渠道汇总", "线上／线下／其他", "线上/线下/其他", "客户主数据分类", "箱规", "返回", "数量", "以及", "和")
    text = re.sub("|".join(re.escape(token.casefold()) for token in sorted(clauses,key=len,reverse=True)),"",text)
    scope, _, unsupported = resolver.resolve(restore_sku_references(text, sku_codes))
    if unsupported or not scope.finished_goods or scope.channel != "ALL":
        return None
    return SemanticRequest(route="sales_outbound_boxes",start=interval[0],end=interval[1],scope=scope,
                           sales_aggregation=SalesAggregation(("ship_date","warehouse","sku","customer_channel"),ref_order_type="JYCK"),
                           ref_order_type="JYCK",finished_goods_only=True)


def filtered_outbound_box_request(question, *, interval, resolver, contract_root):
    """Handle ordinary channel box requests with the approved actual-outbound basis."""

    sku_codes, semantic_question = extract_sku_references(question)
    text = re.sub(r"\s+", "", semantic_question).casefold()
    channel = next((value for value in ("线上", "线下", "其他") if value + "渠道" in text), None)
    if not interval or channel is None or "出库" not in text or "箱" not in text:
        return None
    if not any(token in text for token in ("数量", "出库量", "单量", "单数")):
        return None
    if re.search(r"按|各|每|排名|top|占比|累计|同比|环比|替换|计划|请求|金额|2c|2b|四舍五入|取整|保留", text):
        return None
    path = contract_root / "metrics" / "sales.outbound_boxes.yaml"
    contract = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if contract.get("status") != "active" or contract.get("approval", {}).get("state") != "approved":
        return None
    order_types = tuple(dict.fromkeys(value.upper() for value in re.findall(
        r"(?<![a-z0-9])(jyck|hhck|bfck|qtck|so\d+)(?![a-z0-9])", text
    )))
    scope_question = re.sub(
        re.escape(channel + "渠道") + r"|看下|看一下|查询|统计|计算|返回|展示|显示|出库数量|出库量|数量|单量|单数|箱数|箱|出库",
        "",
        semantic_question,
    )
    scope, _, unsupported = resolver.resolve(restore_sku_references(scope_question, sku_codes))
    if unsupported:
        return None
    return SemanticRequest(
        route="sales_outbound_boxes_channel",
        start=interval[0],
        end=interval[1],
        scope=replace(scope, channel=channel),
        sales_aggregation=SalesAggregation(
            ("ship_date", "warehouse", "sku", "customer_channel"),
            ref_order_types=order_types,
            channel_filter=channel,
        ),
        finished_goods_only=scope.finished_goods,
    )
