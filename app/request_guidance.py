"""Server-owned explanations; never execute a query or carry live-result evidence."""

import re
import unicodedata

from .query_outcomes import DorisResultError


def is_query_outcome_explanation(question: str) -> bool:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(question or ""))).casefold()
    if not re.search(r"回复模板|回复示例|说明|解释|什么意思|是什么|区别", text):
        return False
    if not re.search(r"拒绝|失败|无数据|无业务数据|截断|无记录", text):
        return False
    # Mixed data-and-explanation requests still belong to normal planning.
    if re.search(r"库存|周转|出库|入库|订单|销售|采购|退货|sku|仓库|货主|客户|渠道|dms|积分|今天|昨天|昨日|上月|本月|20\d{2}[-/年]", text):
        return False
    return True


def query_outcome_guidance(question: str) -> dict[str, str] | None:
    if not is_query_outcome_explanation(question):
        return None
    rejected = DorisResultError("backend_rejected").message
    failed = DorisResultError("query_failed").message
    truncated = DorisResultError("result_truncated").message
    text = (
        f"后端拒绝：{rejected}。可能涉及访问权限或查询结构限制，不代表业务没有数据；应先核对允许的查询范围。\n\n"
        f"执行失败：{failed}。应区分连接或超时故障，以及字段、类型、日期表达式等问题；修正原因后重试，不能凭错误推断数量。\n\n"
        "成功无数据：查询已成功执行，但当前筛选范围没有匹配记录。这不等于连接失败，也不能据此编造数量为零。\n\n"
        f"结果截断：{truncated}。先给完整范围的汇总，再按月、仓库或商品分段取明细；日期边界使用左闭右开，分段合计须与总计一致。"
    )
    return {"format": "plain_text", "kind": "query_outcomes", "title": "查询状态说明", "text": text}


def is_large_result_explanation(question: str) -> bool:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(question or ""))).casefold()
    if not re.search(r"结果(?:达到|超过)?(?:行数)?上限|结果截断|大结果集|拆分查询|分段查询|超限", text):
        return False
    if not re.search(r"出库明细|销售明细|发货明细|近一年|按月|按仓库|按sku", text):
        return False
    if not re.search(r"方案|办法|策略|如何处理|怎么处理|怎么办|(?:说明|解释).{0,30}(?:上限|截断)", text):
        return False
    # Mixed requests still require data. A plan is not a substitute for the
    # execution the user explicitly requested after or alongside it.
    actions = re.sub(r"后端返回(?:的)?结果(?:达到|超过)(?:行数)?上限|后端返回(?:的)?结果已?截断", "", text)
    if re.search(
        r"(?:然后|并|再|直接|实际|立即)执行|执行查询|查出来|取出来|不要只|别只"
        r"|(?:继续|重试).{0,30}(?:查询|取数)|把.{0,30}(?:结果|数据).{0,8}给我"
        r"|(?:然后|接着|并|再|随后)(?:按[^，。；;]{1,15})?(?:查询|取数|拉取|提取|导出|统计)"
        r"|[，,。；;](?:请)?按[^，。；;]{1,15}(?:查询|取数|拉取|提取)"
        r"|(?:返回|展示|输出)(?:全部|完整|表格)?(?:数据|结果|明细)", actions
    ):
        return False
    if re.search(r"(?:不要|无需|暂不)执行|^(?:请)?只(?:给出?|需|要).{0,80}(?:方案|办法|说明)", text):
        return True
    # A leading query command is only background when it describes a reported
    # limit failure. Otherwise the normal planner must handle the whole request.
    if re.search(r"^(?:请)?(?:先)?(?:查询|统计|获取|取出|按.{1,10}(?:查询|取数))", text):
        return bool(re.search(r"后端返回.{0,20}(?:上限|截断)", text))
    return True


def large_result_guidance(question: str) -> dict[str, str] | None:
    """Describe partitioning only when the user asks for an explanation."""
    if not is_large_result_explanation(question):
        return None
    return {
        "format": "plain_text",
        "kind": "large_result_plan",
        "title": "大结果集处理方案",
        "text": (
            "明细超过单次完整展示上限时，不能把截断结果当成完整数据。"
            "处理顺序是：先按原口径生成范围汇总和完整性控制；再选择一种分段方式："
            "按月适合时间趋势，按仓库适合仓间核对，按 SKU 适合商品明细。"
            "各分段继承原请求的口径，只改变分段维度；日期使用左闭右开边界，分段不能遗漏或重叠。"
            "数量按不重叠分段求和；出库单数和 SKU 数必须在全范围按业务键去重，不能直接相加。"
            "每批完整校验后才可展示，并标明已取回行数与总行数；只有全部批次与原范围对账后才算完整。"
            "任一批超限、数据变化或对账不一致时，不展示该批，不把之前已校验的批次冒充完整结果。"
        ),
    }
