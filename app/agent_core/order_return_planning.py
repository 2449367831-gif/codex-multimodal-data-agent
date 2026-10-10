"""Request-local order-return planning; no database or monetary inference."""

from datetime import datetime, time, timedelta
import re
import unicodedata

from ..request_intent import assess_request
from .order_components import OrderComponentPlan
from .semantic_types import SemanticRequest, SemanticRuntimeError, _normalize_selected_answers, _next_month


PROMPTS = {
    'return_measure': '本次退货分析需要哪些结果？金额口径尚未确认。',
    'return_month_window': '本月按销售订单创建时间统计到哪个时点？',
    'return_owner_scope': '同时要求全部货主和指定货主，本次采用哪个范围？',
    'return_order_scope': '同时要求全部订单和指定订单，本次采用哪个范围？',
    'return_sku_scope': '同时要求全部商品和指定商品，本次采用哪个范围？',
    'return_date_scope': '同时要求全部时间和指定时间，本次采用哪个范围？',
}
LABELS = {
    'return_quantity_only': '本次只查实发、退货和净销售数量，不含金额',
    'return_proportional_payment': '按退货数量比例扣减实付金额（待确认，暂不执行）',
    'return_actual_refund': '实际退款金额（待确认，暂不执行）',
    'return_month_now': '本月1日00:00至提问时点',
    'return_month_midnight': '本月1日00:00至提问当天00:00',
    'return_month_full': '完整自然月：本月1日至下月1日00:00',
    'return_owner_all': '全部货主',
    'return_owner_list': '仅本次明确列出的货主编码',
    'return_order_all': '全部销售订单',
    'return_order_list': '仅本次明确列出的销售订单编码',
    'return_sku_all': '全部商品',
    'return_sku_list': '仅本次明确列出的商品编码',
    'return_date_all': '全部订单创建时间',
    'return_date_explicit': '仅本次明确给出的订单创建时间区间',
}
OPTIONS = {
    'return_measure': ('return_quantity_only', 'return_proportional_payment', 'return_actual_refund'),
    'return_month_window': ('return_month_now', 'return_month_midnight', 'return_month_full'),
    'return_owner_scope': ('return_owner_list', 'return_owner_all'),
    'return_order_scope': ('return_order_list', 'return_order_all'),
    'return_sku_scope': ('return_sku_list', 'return_sku_all'),
    'return_date_scope': ('return_date_explicit', 'return_date_all', 'return_month_now', 'return_month_midnight', 'return_month_full'),
}
CODE = r'''(?:`([^`]+)`|"([^"]+)"|'([^']+)'|([A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}))'''
FIELD = re.compile(r'(销售订单(?:号|编码)?|货主(?:编码)?|(?<![A-Za-z0-9_])SKU(?:编码)?)\s*(?:为|是|[:：=])?\s*', re.I)
VALUE = re.compile(CODE)
SEPARATOR = re.compile(r'\s*(?:、|，|,|和|与|及)\s*')
DATE = r'(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号])'
PERIOD = re.compile(r'(?:昨天|昨日|前天|今天|今日|上个月|上月|上个自然月|上周|本月|'
    r'(?:最近|近)\d{1,3}个?完整自然日|' + DATE + r'(?:[至到]' + DATE + r'(?:0点前|0点|00:00:00|00:00)?)?)')
PHRASES = (
    '所有销售订单', '全部销售订单', '销售订单创建时间', '订单创建时间',
    '所有货主', '全部货主', '所有SKU', '全部SKU', '全部时间', '所有时间',
    '关联退货单数量', '关联退货单数', '扣退后的实付金额', '扣退后实付金额',
    '净销售数量', '原实发数量', '实发数量', '退货数量', '实退数量',
    '退货金额', '退款金额', '实付金额', '退货情况', '销售订单', '有效退货明细',
    '退货明细', '退货', '按销售订单加SKU汇总', '按销售订单和SKU汇总',
    '查询', '统计', '查看', '返回', '计算', '用表格', '表格', '的', '和', '与', '并', '请',
)


def return_quantity_contract_active(root):
    from .handlers.order_returns import return_quantity_contract_active as active
    return root is not None and active(root)


def is_order_return_question(question):
    return (isinstance(question, str) and '销售订单' in question
            and bool(re.search(r'退货|扣退|实退|净销售数量', question))
            and assess_request(question).kind in {'data_query', 'clarification'})


def _parse(question):
    if not is_order_return_question(question):
        return None
    codes = {'orders': [], 'owners': [], 'skus': []}
    chunks, kinds, cursor, kept = [], [], 0, 0
    while (field := FIELD.search(question, cursor)) is not None:
        cursor = field.end()
        value = VALUE.match(question, cursor)
        if value is None or (value[4] is not None and value[4].casefold() == 'sku'):
            continue
        kind = 'orders' if field[1].startswith('销售订单') else 'owners' if field[1].startswith('货主') else 'skus'
        while value is not None:
            codes[kind].append(next(v for v in value.groups() if v is not None))
            cursor = value.end()
            separator = SEPARATOR.match(question, cursor)
            if separator is None or FIELD.match(question, separator.end()):
                break
            following = VALUE.match(question, separator.end())
            if following is None or (following[4] is not None and following[4].casefold() == 'sku'):
                break
            value = following
        chunks.extend((question[kept:field.start()], ' '))
        kept = cursor
        if not kinds or kinds[-1] != kind:
            kinds.append(kind)
    chunks.append(question[kept:])
    # Repeated dimensions separated by another dimension can describe paired
    # predicates. Flattening them would silently produce a Cartesian scope.
    if len(set(kinds)) != len(kinds):
        return None
    text = ''.join(chunks)
    codes = {key: tuple(dict.fromkeys(values)) for key, values in codes.items()}
    try:
        OrderComponentPlan(owner_codes=codes['owners'], order_codes=codes['orders'], sku_codes=codes['skus'])
    except ValueError:
        return None
    measure_choice = bool(re.search(r'金额|退款|实付', text)) or not re.search(r'数量|退货单数', text)
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    residue = re.sub('|'.join(map(re.escape, sorted(PHRASES, key=len, reverse=True))), '', compact, flags=re.I)
    period = re.sub(r'[，。；、,;？?！!]', '', residue)
    if period and not PERIOD.fullmatch(period):
        return None
    conflicts = tuple(key for key, label in (('owners', '货主'), ('orders', '销售订单'), ('skus', 'SKU'))
                      if codes[key] and re.search(r'(?:全部|所有)' + label, compact, re.I))
    if period and re.search(r'(?:全部|所有)时间', compact):
        conflicts += ('date',)
    return codes, period, measure_choice, conflicts


def _questions(parsed, now):
    _, period, measure_choice, conflicts = parsed
    questions = []
    decisions = [('return_measure', measure_choice)]
    decisions.extend((f'return_{name}_scope', kind in conflicts) for name, kind in
                     (('owner', 'owners'), ('order', 'orders'), ('sku', 'skus'), ('date', 'date')))
    decisions.append(('return_month_window', period == '本月' and 'date' not in conflicts))
    for key, needed in decisions:
        if needed:
            options = OPTIONS[key]
            if key == 'return_date_scope':
                options = (OPTIONS['return_month_window'] + ('return_date_all',) if period == '本月'
                           else ('return_date_explicit', 'return_date_all'))
            if key in {'return_month_window', 'return_date_scope'} and now.day == 1:
                options = tuple(option for option in options if option != 'return_month_midnight')
                if (now.hour, now.minute, now.second) == (0, 0, 0):
                    options = tuple(option for option in options if option != 'return_month_now')
            questions.append(dict(id=key, prompt=PROMPTS[key], selection='single',
                                 options=[dict(id=option, label=LABELS[option]) for option in options]))
    return questions


def return_request(question, *, planner, contract_root, selected=None):
    parsed = _parse(question)
    if parsed is None or not return_quantity_contract_active(contract_root):
        return None
    now = planner._now().replace(microsecond=0)
    questions = _questions(parsed, now)
    selected = selected or {}
    if set(selected) != {q['id'] for q in questions} or any(
            selected[q['id']] not in {o['id'] for o in q['options']} for q in questions):
        return None
    if selected.get('return_measure', 'return_quantity_only') != 'return_quantity_only':
        return None
    codes, period, _, _ = parsed
    codes = dict(codes)
    for name, kind in (('owner', 'owners'), ('order', 'orders'), ('sku', 'skus')):
        if selected.get(f'return_{name}_scope') == f'return_{name}_all':
            codes[kind] = ()
    if selected.get('return_date_scope') == 'return_date_all':
        period = ''
    start = end = None
    if period == '本月':
        start = now.replace(day=1, hour=0, minute=0, second=0)
        option = selected.get('return_month_window') or selected['return_date_scope']
        year, month = _next_month(now.year, now.month)
        end = (now if option == 'return_month_now' else now.replace(hour=0, minute=0, second=0)
               if option == 'return_month_midnight' else start.replace(year=year, month=month))
    elif period:
        try:
            explicit = re.fullmatch('(' + DATE + r')[至到](' + DATE + r')(0点前|0点|00:00:00|00:00)?', period)
            if explicit:
                first = planner._parse_explicit_date(explicit[1])
                last = planner._parse_explicit_date(explicit[2])
                if first is None or last is None:
                    return None
                interval = first, last + timedelta(days=0 if explicit[3] else 1)
            else:
                interval = planner._source_interval(period)
        except (ValueError, SemanticRuntimeError):
            return None
        if interval is None:
            return None
        start, end = (datetime.combine(day, time.min, tzinfo=now.tzinfo) for day in interval)
    try:
        plan = OrderComponentPlan(owner_codes=codes['owners'], order_codes=codes['orders'],
                                  sku_codes=codes['skus'], start=start, end=end)
    except ValueError:
        return None
    return SemanticRequest('sales_order_net_quantity', start, end, scope=plan.execution_scope(), order_return_plan=plan)


def return_clarification(question, *, contract_root):
    parsed = _parse(question)
    if parsed is None or not return_quantity_contract_active(contract_root):
        return None
    from .planning import RequestPlanner
    planner = RequestPlanner()
    questions = _questions(parsed, planner._now())
    if not questions:
        return None
    sample = {q['id']: q['options'][0]['id'] for q in questions}
    if return_request(question, planner=planner, contract_root=contract_root, selected=sample) is None:
        return None
    return {'outcome': 'clarification_required', 'questions': questions}


def return_block_reason(question, *, contract_root, selected=None):
    if not is_order_return_question(question):
        return None
    if not return_quantity_contract_active(contract_root):
        return '订单退货数量口径尚未审批发布，未执行查询；金额口径仍待确认。'
    if _parse(question) is None:
        return '当前订单退货查询的范围或条件尚未接入，未删减条件执行；请通过新口径入口提交该需求。'
    chosen = (_normalize_selected_answers(selected) or {}) if selected is not None else {}
    if chosen.get('return_measure') in {'return_proportional_payment', 'return_actual_refund'}:
        return '已保留金额需求；金额口径尚未确认，未执行查询，也未替换成仅数量结果。'
    if selected is not None:
        return '本次退货选择尚未形成可执行组合，未扩大范围或重新询问。'
    from .planning import RequestPlanner
    planner = RequestPlanner()
    sample = {q['id']: q['options'][0]['id'] for q in _questions(_parse(question), planner._now())}
    if return_request(question, planner=planner, contract_root=contract_root, selected=sample) is None:
        return '订单退货查询的日期范围无效，未执行查询，也未替换时间范围。'
    return None
