"""Collect explicit competing turnover proposals without publishing them."""

from collections.abc import Mapping
from datetime import date
import re

from .semantic_types import _normalize_selected_answers


PROMPTS = {
    'tc_variant': '请选择本次讨论的历史方案；选择不代表口径已发布。',
    'tc_stock': '库存采用哪个时点？',
    'tc_sales': '销售数量采用哪个业务口径？',
    'tc_returns': '销售数量是否扣减退货？',
    'tc_scope': '货主、仓库范围采用哪一种？',
    'tc_window': '销售日均量采用哪个时间区间？',
}
OPTIONS = {
    'tc_variant': ('tc_a', 'tc_b'),
    'tc_stock': ('tc_realtime', 'tc_month_start'),
    'tc_sales': ('tc_actual', 'tc_allocated'),
    'tc_returns': ('tc_keep_returns', 'tc_deduct_returns'),
    'tc_scope': ('tc_all', 'tc_specified'),
    'tc_window': ('tc_7days', 'tc_mtd', 'tc_custom'),
}
LABELS = {
    'tc_a': 'A：当前实时可用库存 / 近7天日均销售出库',
    'tc_b': 'B：月初库存与本月累计出库（公式待确认）',
    'tc_realtime': '当前实时可用库存（不含在途）',
    'tc_month_start': '本月月初历史库存快照',
    'tc_actual': '实际完成的销售出库数量',
    'tc_allocated': '分配数量（单位关系未确认）',
    'tc_keep_returns': '不扣减退货，按出库量统计',
    'tc_deduct_returns': '扣减退货（退货口径待确认）',
    'tc_all': '全部货主、全部仓库；其余未说明范围均为全部',
    'tc_specified': '指定范围（未明确的范围暂不执行）',
    'tc_7days': '最近7个完整自然日',
    'tc_mtd': '本自然月1日至提问当天00:00',
    'tc_custom': '指定日期区间',
}
DATE_RANGE_OPTIONS = {'tc_window': 'tc_custom'}
REASONS = (
    '库存时点与方案不一致',
    '销售区间与方案不一致',
    '分配数量单位待确认',
    '退货扣减口径待确认',
    '指定范围尚未形成完整查询条件',
)


def has_turnover_conflict(question):
    if not isinstance(question, str):
        return False
    text = re.sub(r'\s+', '', question).casefold()
    # Only recognize the two defined proposals, not any ordinary turnover query.
    return all((
        '库存' in text and '周转' in text,
        any(marker in text for marker in ('两个历史说法', '两种口径', '口径冲突', '两个方案', '两种说法')),
        '月初库存' in text and '本月累计出库' in text,
        '实时可用库存' in text and re.search(r'(?:近|最近)7[天日]', text),
    ))


def turnover_conflict_clarification(question):
    if not has_turnover_conflict(question):
        return None
    questions = [
        {'id': key, 'prompt': prompt, 'selection': 'single',
         'options': [{'id': option, 'label': LABELS[option]} for option in OPTIONS[key]]}
        for key, prompt in PROMPTS.items()
    ]
    questions[-1]['date_range_option'] = 'tc_custom'
    return {'outcome': 'clarification_required', 'questions': questions}


def validate_date_range(question_id, option_ids, answer):
    """Accept exact ISO calendar dates only for a registered selected option."""
    expected = DATE_RANGE_OPTIONS.get(question_id)
    required = expected is not None and expected in option_ids
    if not required:
        if 'date_range' in answer:
            raise ValueError('date range is not allowed for this option')
        return None
    value = answer.get('date_range')
    if not isinstance(value, Mapping) or set(value) != {'start', 'end'}:
        raise ValueError('date range requires start and exclusive end')
    for item in value.values():
        if not isinstance(item, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', item):
            raise ValueError('invalid date range')
    start, end = date.fromisoformat(value['start']), date.fromisoformat(value['end'])
    if start >= end:
        raise ValueError('date range must have positive length')
    return {'start': start.isoformat(), 'end': end.isoformat()}


def selected_turnover_conflict_reason(source, selections):
    selected = _normalize_selected_answers(selections)
    if not has_turnover_conflict(source) or not selected or set(selected) != set(OPTIONS):
        return None
    if any(selected[key] not in options for key, options in OPTIONS.items()):
        return None
    try:
        for answer in selections:
            key = answer.get('question_id') or answer.get('id')
            validate_date_range(key, [selected[key]], answer)
    except (ValueError, TypeError, KeyError):
        return None
    is_a = selected['tc_variant'] == 'tc_a'
    failures = (
        selected['tc_stock'] != ('tc_realtime' if is_a else 'tc_month_start'),
        selected['tc_window'] != ('tc_7days' if is_a else 'tc_mtd'),
        selected['tc_sales'] == 'tc_allocated',
        selected['tc_returns'] == 'tc_deduct_returns',
        selected['tc_scope'] == 'tc_specified',
    )
    reasons = ['历史方案尚未发布，不能替代已确认的成品库存加在途口径']
    reasons.extend(reason for reason, failed in zip(REASONS, failures) if failed)
    return '已保留本次选择。' + '；'.join(reasons) + '。本次未执行查询，无需重复选择。'
