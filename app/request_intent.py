"""Request-kind assessment, independent of metric selection and live results.

This layer supplies routing constraints, not SQL or business answers. Unknown
wording belongs to the model planner; it is never evidence of missing scope.
"""

from dataclasses import asdict, dataclass
import re
import unicodedata
from .request_guidance import is_large_result_explanation, is_query_outcome_explanation


@dataclass(frozen=True, slots=True)
class RequestAssessment:
    kind: str
    domains: tuple[str, ...]
    requires_time_window: bool

    def as_dict(self):
        return asdict(self)


def assess_request(question: str) -> RequestAssessment:
    text = unicodedata.normalize("NFKC", str(question or "")).casefold()
    compact = re.sub(r"\s+", "", text)
    domains = []
    for domain, markers in (
        ("dms", ("dms", "积分兑换", "积分订单")),
        ("inventory", ("库存", "在途", "占用", "周转", "待入库", "待出库")),
        ("returns", ("退货", "扣退", "实退")),
        ("inbound", ("采购入库", "实收入库", "入库明细")),
        ("sales_order", ("销售订单", "订单请求", "so07", "请求数量")),
        ("sales", ("销售", "出库", "出货", "发货", "发出", "发了", "销量", "动销", "卖了", "卖出", "售出")),
        ("warehouse", ("仓库", "仓编码", "仓名称")),
        ("sku", ("sku", "商品", "箱规", "货品")),
        ("customer", ("客户", "店铺")),
    ):
        if any(marker in compact for marker in markers):
            domains.append(domain)
    values = tuple(domains) or ("unknown",)
    if is_query_outcome_explanation(question) or is_large_result_explanation(question):
        kind = "explanation"
    elif re.search(r"(?:列出|给出|返回).{0,20}(?:确认|澄清).{0,20}(?:选项|口径|问题)|不要.{0,4}猜.{0,12}选项", compact):
        kind = "clarification"
    elif ("mysql" in compact and re.search(r"改写|转换|改成|迁移", compact)):
        kind = "sql_conversion"
    elif re.search(r"变更|旧sql|新需求|新口径", compact) and re.search(r"影响|对比|差异|改|变更", compact):
        kind = "change_assessment"
    elif re.search(r"排查|诊断|检查.{0,50}异常|数据.{0,12}不一致", compact):
        kind = "diagnostic"
    elif re.search(r"^(?:请)?(?:解释|说明)|给出识别规则|判断哪些|回复模板", compact) and not re.search(r"查询|统计|返回.{0,12}(?:数量|金额)", compact):
        kind = "explanation"
    elif (
        any(domain in values for domain in ("warehouse", "sku", "customer"))
        and re.search(r"名称|编码|类型|节点|主数据|箱规", compact)
        and not re.search(r"库存|在途|出库|订单|销售|销量|金额|退货|入库|占用", compact)
    ):
        kind = "lookup"
    elif re.search(r"库存|在途|出库|出货|发货|发出|发了|订单|销售|销量|动销|卖了|卖出|售出|退货|入库|占用|收货", compact) or ("dms" in compact and "兑换" in compact):
        kind = "data_query"
    else:
        kind = "unknown"
    return RequestAssessment(kind, values, kind == "data_query" and "inventory" not in values)
