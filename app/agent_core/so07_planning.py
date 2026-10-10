"""Request-local SO07 intent and selectable scope conflicts; no data access."""

from datetime import date, timedelta
import re
import unicodedata

from ..request_intent import assess_request
from .sales_order_details import SalesOrderDetailPlan
from .semantic_types import SemanticRequest, SemanticRuntimeError, _next_month


PROMPTS = {
    'so07_month_window': '本月按订单创建时间采用哪个自然日区间？',
    'so07_date_scope': '同时要求全部时间和指定时间，本次采用哪个范围？',
    'so07_owner_scope': '同时要求全部货主和指定货主，本次采用哪个范围？',
    'so07_warehouse_scope': '同时要求全部仓库和指定仓库，本次采用哪个范围？',
    'so07_sku_scope': '同时要求全部商品和指定商品，本次采用哪个范围？',
}
LABELS = {
    'so07_month_full': '完整自然月：本月1日至下月1日00:00',
    'so07_month_midnight': '本月1日00:00至提问当天00:00',
    'so07_month_including_today': '本月1日00:00至提问次日00:00（含提问当天）',
    'so07_date_explicit': '仅本次明确的订单创建时间区间',
    'so07_date_all': '全部订单创建时间',
    'so07_owner_list': '仅本次指定货主', 'so07_owner_all': '全部货主',
    'so07_warehouse_list': '仅本次指定仓库编码', 'so07_warehouse_all': '全部仓库',
    'so07_sku_list': '仅本次指定SKU编码', 'so07_sku_all': '全部商品',
}
OPTIONS = {
    'so07_month_window': ('so07_month_full', 'so07_month_midnight', 'so07_month_including_today'),
    'so07_date_scope': ('so07_date_explicit', 'so07_date_all', 'so07_month_full', 'so07_month_midnight', 'so07_month_including_today'),
    **{f'so07_{key}_scope': (f'so07_{key}_list', f'so07_{key}_all') for key in ('owner', 'warehouse', 'sku')},
}
FIELD = re.compile(r'(货主(?:编码)?|仓库编码|(?<![A-Za-z0-9_])SKU(?:编码)?)\s*(?:为|是|[:：=])?\s*', re.I)
VALUE = re.compile(r'''(?:`([^`]+)`|"([^"]+)"|'([^']+)'|([A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}))''')
SEPARATOR = re.compile(r'\s*(?:、|，|,|和|与|及)\s*')
DATE = r'(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号])'
PERIOD = re.compile(r'(?:昨天|昨日|前天|今天|今日|上个月|上月|上个自然月|上周|本月|'
                    r'(?:最近|近)\d{1,3}个?完整自然日|' + DATE + r'(?:[至到]' + DATE + r'(?:0点前|0点|00:00:00|00:00)?)?)')
PHRASES = (
    '不要把普通JYCK订单混进来', '订单号必须是SO07', '订单类型必须是SO07', '订单类型为SO07',
    'SO07社团销售订单', 'SO07销售订单', 'SO07社团订单', 'SO07订单', '社团销售订单', '社团订单',
    '全部订单创建时间', '所有订单创建时间', '全部货主', '所有货主', '全部仓库', '所有仓库',
    '全部SKU', '所有SKU', '全部商品', '所有商品', '全部客户', '所有客户', '全部渠道', '所有渠道',
    '全部时间', '所有时间', '销售订单号', '订单创建时间', '订单状态', '原始请求数量', '请求数量',
    '源数量单位代码', '源销售单位代码', '源单位代码', '不跨单位合计', '不换算',
    '客户编码', '客户名称', '仓库编码', '仓库名称', 'SKU编码', 'SKU名称',
    '销售订单', '订单明细', '明细', '客户', '仓库', 'SKU',
    '查询', '统计', '查看', '返回', '展示', '用表格', '表格', '北京时间', '自然日', '的', '和', '与', '并', '请',
)


def so07_contract_active(root):
    from .handlers.so07 import so07_contract_active as active
    return active(root)


def is_so07_question(question):
    return (isinstance(question, str) and bool(re.search(
        r'SO07\s*(?:社团)?(?:销售)?订单|社团(?:销售)?订单|订单(?:号|类型)[^，。；\n]{0,12}\bSO07\b', question, re.I))
        and assess_request(question).kind in {'data_query', 'clarification'})


def _parse(question):
    if not is_so07_question(question):
        return None
    codes = {'owner': [], 'warehouse': [], 'sku': []}
    chunks, seen, cursor, kept = [], [], 0, 0
    while (field := FIELD.search(question, cursor)) is not None:
        cursor = field.end()
        value = VALUE.match(question, cursor)
        if value is None or (value[4] is not None and value[4].upper() in {'SKU', 'SO07'}):
            continue
        kind = 'owner' if field[1].startswith('货主') else 'warehouse' if field[1].startswith('仓库') else 'sku'
        while value is not None:
            codes[kind].append(next(v for v in value.groups() if v is not None))
            cursor = value.end()
            separator = SEPARATOR.match(question, cursor)
            if separator is None or FIELD.match(question, separator.end()):
                break
            value = VALUE.match(question, separator.end())
            if value is not None and value[4] is not None and value[4].upper() in {'SKU', 'SO07'}:
                break
        chunks.extend((question[kept:field.start()], ' '))
        kept = cursor
        if not seen or seen[-1] != kind:
            seen.append(kind)
    chunks.append(question[kept:])
    if len(seen) != len(set(seen)):
        return None
    codes = {key: tuple(dict.fromkeys(values)) for key, values in codes.items()}
    if len(codes['owner']) > 1:
        return None
    try:
        SalesOrderDetailPlan(None, None, owner=codes['owner'][0] if codes['owner'] else 'ALL',
                             warehouse_codes=codes['warehouse'], sku_codes=codes['sku'])
    except ValueError:
        return None
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', ''.join(chunks)))
    residue = re.sub('|'.join(map(re.escape, sorted(PHRASES, key=len, reverse=True))), '', text, flags=re.I)
    period = re.sub(r'[，。；、,;？?！!]', '', residue)
    if period and not PERIOD.fullmatch(period):
        return None
    conflicts = tuple(key for key, noun in (('owner', '货主'), ('warehouse', '仓库'), ('sku', '(?:SKU|商品)'))
                      if codes[key] and re.search(r'(?:全部|所有)' + noun, text, re.I))
    if period and re.search(r'(?:全部|所有)(?:订单创建)?时间', text):
        conflicts += ('date',)
    return codes, period, conflicts


def _questions(parsed, today):
    _, period, conflicts = parsed
    result = []
    keys = [f'so07_{key}_scope' for key in conflicts]
    if period == '本月' and 'date' not in conflicts:
        keys.append('so07_month_window')
    for key in keys:
        options = OPTIONS[key]
        if key == 'so07_date_scope':
            options = OPTIONS['so07_month_window'] + ('so07_date_all',) if period == '本月' else ('so07_date_explicit', 'so07_date_all')
        if today.day == 1:
            options = tuple(o for o in options if o != 'so07_month_midnight')
        if (today + timedelta(days=1)).day == 1:
            options = tuple(o for o in options if o != 'so07_month_including_today')
        result.append(dict(id=key, prompt=PROMPTS[key], selection='single',
                           options=[dict(id=o, label=LABELS[o]) for o in options]))
    return result


def so07_request(question, *, planner, contract_root, selected=None):
    parsed = _parse(question)
    if parsed is None or not so07_contract_active(contract_root):
        return None
    today = planner._now().date()
    questions = _questions(parsed, today)
    selected = selected or {}
    if set(selected) != {q['id'] for q in questions} or any(
            selected[q['id']] not in {o['id'] for o in q['options']} for q in questions):
        return None
    codes, period, _ = parsed
    codes = dict(codes)
    for kind in codes:
        if selected.get(f'so07_{kind}_scope') == f'so07_{kind}_all':
            codes[kind] = ()
    if selected.get('so07_date_scope') == 'so07_date_all':
        period = ''
    start = end = None
    if period == '本月':
        start = today.replace(day=1)
        choice = selected.get('so07_month_window') or selected['so07_date_scope']
        end = (date(*_next_month(today.year, today.month), 1) if choice == 'so07_month_full' else
               today + timedelta(days=1) if choice == 'so07_month_including_today' else today)
    elif period:
        try:
            explicit = re.fullmatch('(' + DATE + r')(?:[至到](' + DATE + r')(0点前|0点|00:00:00|00:00)?)?', period)
            if explicit:
                first = planner._parse_explicit_date(explicit[1])
                last = planner._parse_explicit_date(explicit[2]) if explicit[2] else first
                if first is None or last is None:
                    return None
                interval = first, last + timedelta(days=0 if explicit[3] else 1)
            else:
                interval = planner._source_interval(period)
        except (ValueError, SemanticRuntimeError):
            return None
        if interval is None:
            return None
        start, end = interval
    try:
        plan = SalesOrderDetailPlan(start, end, owner=codes['owner'][0] if codes['owner'] else 'ALL',
                                   warehouse_codes=codes['warehouse'], sku_codes=codes['sku'])
    except ValueError:
        return None
    return SemanticRequest('so07_order_details', start, end, scope=plan.execution_scope(), so07_plan=plan)


def so07_clarification(question, *, contract_root, planner=None):
    parsed = _parse(question)
    if parsed is None or not so07_contract_active(contract_root):
        return None
    if planner is None:
        from .planning import RequestPlanner
        planner = RequestPlanner()
    questions = _questions(parsed, planner._now().date())
    if not questions:
        return None
    sample = {q['id']: q['options'][0]['id'] for q in questions}
    if so07_request(question, planner=planner, contract_root=contract_root, selected=sample) is None:
        return None
    return {'outcome': 'clarification_required', 'questions': questions}


def so07_block_reason(question, *, contract_root, selected=None):
    if not is_so07_question(question):
        return None
    if not so07_contract_active(contract_root):
        return '社团订单口径尚未审批发布，未执行查询。'
    parsed = _parse(question)
    if parsed is None:
        return '本次社团订单的条件或输出要求尚未接入，未删减条件查询；请通过新口径入口提交该需求。'
    if selected is not None:
        return '本次社团订单选择未形成可执行范围，未扩大范围或重新询问。'
    from .planning import RequestPlanner
    planner = RequestPlanner()
    sample = {q['id']: q['options'][0]['id'] for q in _questions(parsed, planner._now().date())}
    if so07_request(question, planner=planner, contract_root=contract_root, selected=sample) is None:
        return '社团订单日期范围无法确认，未执行查询，也未替换时间范围。'
    return None
