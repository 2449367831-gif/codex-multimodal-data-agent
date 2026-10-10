"""Explicit unpublished comparisons must not fall through to model execution."""

from pathlib import Path
import re
import unicodedata

import yaml

from ..knowledge_activation import active_snapshot
from ..request_intent import assess_request
from ..errors import RuntimeErrorBase


class UnpublishedComparisonError(RuntimeErrorBase):
    code = "business_definition_unpublished"
    http_status = 409

    def __init__(self):
        super().__init__("分配数量与实际出库数量的单位关系尚未确认，差异口径暂未发布，本次未执行查询。实际出库量可单独查询。")

    def as_public_dict(self):
        return {"code": self.code, "message": self.message, "result_state": "blocked", "details": {}}


def without_excluded_allocation_source(question: str) -> str:
    """Consume only explicit non-use clauses, not filters or comparisons."""
    return re.sub(
        r"(?:不要|不查|不需要|不含|不能|不使用|不采用|禁止使用|不要用|切勿使用)"
        r"(?:查询|统计|展示|比较|计算|采用|使用|用)?"
        r"(?:分配(?:数量|量)(?:[（(]allocate_qty[）)])?|allocate_qty)"
        r"(?=$|[，,。.!！?？;；])",
        "", question, flags=re.IGNORECASE,
    )


def enforce_comparison_contract(question: str, project_root: Path) -> None:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", question)).casefold()
    # A source-selection constraint such as "不要使用分配数量" explicitly
    # requests actual shipment quantity.  It is not an unpublished comparison
    # and must not trigger the comparison contract.
    text = without_excluded_allocation_source(text)
    if not re.search(r"分配(?:数量|量)|allocate_qty", text):
        return
    if not re.search(r"(?:实际|实发|完成).{0,8}(?:出库|发货|数量)|ship_qty", text):
        return
    kind = assess_request(question).kind
    if kind not in {"data_query", "diagnostic"} and not (
        kind == "unknown" and re.search(r"查询|统计|计算|给出|返回", text)
    ):
        return
    snapshot, _ = active_snapshot(project_root)
    contract = yaml.safe_load((snapshot / "metrics/sales.outbound_actual.v2.yaml").read_text(encoding="utf-8"))
    comparison = contract.get("filters", {}).get("allocation_comparison", {})
    if comparison.get("status") != "active" or comparison.get("unit_relationship") != "confirmed":
        raise UnpublishedComparisonError()
