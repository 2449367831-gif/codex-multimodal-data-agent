"""Request-local relationship intent; identifiers are masked before date parsing."""

from pathlib import Path
import re

import yaml

from ..query_scope import QueryScope
from .outbound_links import OutboundLinkPlan
from .semantic_types import SemanticRequest, SemanticRuntimeError


CODE = r"(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}))"
OUTBOUND = re.compile(r"出库单(?:号|编码)?\s*(?:为|是|[:：=])?\s*" + CODE)
OWNER = re.compile(r"货主\s*(?:为|是|[:：=])?\s*" + CODE)
SIGNAL = re.compile(r"出库.*(?:关联链路|关联关系|来源单号|来源编码)")


def active_link_contract(root):
    if root is None:
        return False
    path = Path(root) / "metrics/sales.outbound_link.yaml"
    if not path.exists():
        return False
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return contract.get("status") == "active" and contract.get("approval", {}).get("state") == "approved"


def is_link_question(question):
    return isinstance(question, str) and SIGNAL.search(question) is not None


def _parse(question):
    if not is_link_question(question):
        return None
    owners, codes = [], []
    def collect(match, target):
        value = next(value for value in match.groups() if value is not None)
        if not value or len(value) > 160 or value != value.strip():
            target.append(None)
        else:
            target.append(value)
        return " "
    text = OUTBOUND.sub(lambda match: collect(match, codes), question)
    text = OWNER.sub(lambda match: collect(match, owners), text)
    if None in codes or None in owners or len(set(owners)) > 1:
        return None
    # These are projection descriptions, not query predicates. Everything else
    # must be an explicit supported scope or date phrase; unknown text is not erased.
    phrases = (
        "并解释这些字段分别来自哪张表", "解释这些字段分别来自哪张表", "订单来源编码",
        "销售订单号", "头部来源单号", "明细来源编码", "来源单号", "来源平台", "出库关联类型",
        "订单类型", "出库单号", "关联链路", "关联关系", "全部货主", "所有货主", "全部时间",
        "出库时间", "查询范围内", "查询", "返回", "查看", "用表格", "表格", "一张", "最近一张",
        "最新一张", "全部", "所有", "出库单", "的", "和", "与", "并", "请",
    )
    residue = re.sub("|".join(map(re.escape, sorted(phrases, key=len, reverse=True))), "", text)
    residue = re.sub(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}|(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号]?", "", residue)
    residue = re.sub(r"昨天|昨日|今天|今日|前天|上个月|上月|本月|最近\d+个?完整自然日|近\d+天|至|到|0点前|0点|00:00:00|00:00", "", residue)
    if re.sub(r"[\s，。；、,;：:？?！!]", "", residue):
        return None
    return tuple(dict.fromkeys(codes)), owners[0] if owners else "ALL", text


def link_request(question, *, interval_parser, contract_root, selected=None):
    parsed = _parse(question)
    if parsed is None or not active_link_contract(contract_root):
        return None
    codes, owner, text = parsed
    try:
        interval = interval_parser(text)
    except SemanticRuntimeError as exc:
        # Invalid explicit dates are a non-match for this optional route;
        # callers that own the generic query route can surface the typed error.
        if exc.reason == "date_invalid":
            return None
        raise
    if interval is None and re.search(r"昨天|今日|今天|昨日|前天|上月|上个月|本月|最近|近\d|20\d{2}|\d+月", text.replace("最近一张", "")):
        return None
    latest = "最新一张" in text or "最近一张" in text
    if selected is not None:
        if codes or latest or selected not in ({"outbound_link_scope": "outbound_link_latest"}, {"outbound_link_scope": "outbound_link_all"}):
            return None
        latest = selected["outbound_link_scope"] == "outbound_link_latest"
    elif not codes and not latest and not re.search(r"(?:全部|所有)出库单", text):
        return None
    start, end = interval or (None, None)
    plan = OutboundLinkPlan(outbound_codes=codes, owner=owner, start=start, end=end, latest=latest)
    return SemanticRequest("sales_outbound_link", start, end, scope=QueryScope(owner=owner, unit="none"), outbound_link_plan=plan)


def link_clarification(question, *, contract_root):
    parsed = _parse(question)
    if parsed is None or not active_link_contract(contract_root):
        return None
    codes, _, text = parsed
    if codes or re.search(r"最新一张|最近一张|(?:全部|所有)出库单", text):
        return None
    from .planning import RequestPlanner
    if link_request(question, interval_parser=RequestPlanner()._source_interval,
                    contract_root=contract_root, selected={"outbound_link_scope": "outbound_link_all"}) is None:
        return None
    return {"outcome": "clarification_required", "questions": [{
        "id": "outbound_link_scope", "prompt": "未指定出库单号，本次查询哪一范围的关联链路？",
        "selection": "single", "options": [
            {"id": "outbound_link_latest", "label": "查询范围内最新出库的一张单"},
            {"id": "outbound_link_all", "label": "查询范围内全部出库单"},
        ],
    }]}
