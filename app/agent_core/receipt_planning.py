"""Deterministic planning for the reviewed purchase-receipt detail route."""

from __future__ import annotations

from datetime import date
import re
from pathlib import Path

import yaml

from ..query_scope import ScopeResolver
from ..sku_references import extract_sku_references, restore_sku_references
from .inbound_details import InboundDetailPlan
from .calendar_window import take_calendar_window
from .semantic_types import SemanticRequest, _compact


def _contract_is_active(root: Path) -> bool:
    try:
        value = yaml.safe_load((root / "metrics/purchase.received_detail.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return False
    approval = value.get("approval") if isinstance(value, dict) else None
    return bool(isinstance(value, dict) and value.get("status") == "active" and isinstance(approval, dict) and approval.get("state") == "approved")


def _basis(text: str) -> str:
    compact = _compact(text)
    has_type = bool(re.search(r"入库类型\s*(?:为|是|=|：|:)?\s*10", compact))
    has_reference = "采购单号非空" in compact or "采购单号不为空" in compact
    if has_type and has_reference:
        return "both"
    if has_reference:
        return "purchase_order_reference"
    return "purchase_type"


def inbound_detail_request(question: str, *, today: date, resolver: ScopeResolver, contract_root: Path,
                           interval_parser) -> SemanticRequest | None:
    """Keep production scope independent from the reviewed arrival cutoff."""
    sku_codes, semantic_question = extract_sku_references(question)
    compact = _compact(semantic_question)
    if not _contract_is_active(contract_root):
        return None
    if "采购入库明细" not in compact or "生产日期" not in compact:
        return None
    cutoff = re.search(r"(?:不包含|不含|不包括|排除)\s*今天\s*(?:完成)?\s*入库\s*(?:的)?\s*(?:数据)?", semantic_question)
    if cutoff is None:
        return None
    window = take_calendar_window(semantic_question[:cutoff.start()] + "," + semantic_question[cutoff.end():],
                                  interval_parser=interval_parser)
    if window is None:
        return None
    scope_text = _compact(window.remainder)
    scope_text = re.sub(r"采购单号(?:非空|不为空)(?:且入库类型(?:为|是|=|:)?10(?!\d))?|入库类型(?:为|是|=|:)?10(?!\d)", "", scope_text)
    # Remove only recognized dimensions, display fields and syntax. Remaining
    # words are checked by the existing scope resolver, never silently ignored.
    for token in ("采购入库明细", "实收入库数量", "实际入库时间", "生产日期", "入库单", "批次", "识别", "返回", "输出", "查询", "数据", "明细", "按", "但", "的", "和"):
        scope_text = scope_text.replace(token, "")
    # Keep ALL declarations and separators until scope validation; removing
    # display fields first can join unrelated words into a different scope.
    scope, shape, unsupported = resolver.resolve(restore_sku_references(scope_text, sku_codes))
    residue = re.sub(r"(?:所有|全部)(?:仓库|商品|sku)|仓库|sku", "", shape)
    residue = re.sub(r"[\s，,。、;；:：]", "", residue)
    if unsupported or residue or scope.product_terms or scope.finished_goods:
        return None
    start, end = window.start, window.end
    return SemanticRequest(
        route="purchase_received_detail", start=start, end=end, scope=scope,
        receipt_plan=InboundDetailPlan(start, end, today, _basis(semantic_question), owner=scope.owner,
                                       warehouse_codes=scope.warehouse_codes, sku_codes=scope.sku_codes),
    )
