"""Explicit actual-sales details; never silently reinterpret all outbound."""

from pathlib import Path
import re

import yaml

from ..knowledge_activation import active_snapshot
from ..sku_references import extract_sku_references, restore_sku_references
from .calendar_window import take_calendar_window
from .semantic_types import SemanticRequest


def actual_details_enabled():
    root, _ = active_snapshot(Path(__file__).resolve().parents[2])
    path = root / 'metrics/sales.outbound_actual.v2.yaml'
    contract = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    return (contract.get('version') == '2.0.0' and contract.get('status') == 'active'
            and contract.get('approval', {}).get('state') == 'approved')


def sales_detail_request(question, *, planner):
    # "销售出库明细" already identifies the released actual-shipment
    # contract.  Requiring the adjective "实际" here sent ordinary business
    # wording to the generic raw-SQL path, where a large day can be truncated
    # before the reviewed batch cursor is created.
    if not re.search(r'(?:实际|有效)?销售出库明细', question):
        return None
    sku_codes, semantic_question = extract_sku_references(question)
    window = take_calendar_window(semantic_question, interval_parser=planner._source_interval)
    if window is None:
        return None
    scope_question = re.sub(r'((?:实际|有效)?销售出库)明细', r'\1数量', window.remainder)
    # Monthly retrieval already uses the bounded partition reader. Consume only
    # that execution modifier; warehouse/SKU partitioning and filters stay intact.
    monthly = r'按月份?(?:拆分|分段|分批)(?:查询|取数|拉取)'
    scope_question = re.sub(r'^\s*(?:请)?' + monthly, '', scope_question)
    scope_question = re.sub(
        r'(?<=[，,。；;])\s*(?:请)?' + monthly + r'(?=\s*(?:$|[，,。；;]))', '', scope_question
    )
    scope_question = re.sub(
        r'(?:用|以)(?:中文)?表格(?:的形式|形式)?(?:展示|显示|输出)'
        r'|(?:展示|显示|输出)(?:中文)?表格', '', scope_question)
    scope, shape, unsupported = planner.scope_resolver.resolve(restore_sku_references(scope_question, sku_codes))
    if unsupported:
        return None
    shape = re.sub(r'[\s,，。？！?!；;]', '', shape)
    if not re.fullmatch(r'(?:请)?(?:帮我)?(?:查询|统计|查一下|查下|看下)?(?:从)?(?:的)?'
                        r'(?:(?:全部|所有)(?:仓库|sku))*(?:产成品|成品)?'
                        r'(?:实际|有效)?销售出库数量', shape):
        return None
    if not actual_details_enabled():
        return None
    return SemanticRequest(route='sales_outbound_details', start=window.start, end=window.end,
                           scope=scope, finished_goods_only=scope.finished_goods)
