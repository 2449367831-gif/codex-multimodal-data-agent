"""Business-approved spreadsheet layout; data and denominators stay separate."""
from decimal import Decimal, ROUND_HALF_UP

from .turnover_report import (
    TurnoverReportError, _load_pillow, _get_font, _draw_center, _draw_left,
    _text_size, _wrap_text, format_days, format_quantity,
)


def render_reference_image(report, *, center_report=None, template=None, font_path=None, width=2200, quality_status=None):
    Image, ImageDraw, ImageFont = _load_pillow()
    center = center_report or report
    if template:
        if center.window_days != 7 or report.window_days != 14:
            raise TurnoverReportError("template requires independent 7/14-day reports")
        if center.window_end_exclusive != report.window_end_exclusive:
            raise TurnoverReportError("template window ends differ")
        def inventory(r):
            return {row.wms_code: (row.available_ea, row.belonging_center_code) for row in r.source_rows}
        if inventory(center) != inventory(report):
            raise TurnoverReportError("template inventory snapshots or warehouse scope differ")
        groups = {g.center.wms_code: g for g in report.groups}
        stacks = [[groups[code] for code in col] for col in template.layout_columns]
        rank = {w.code: w.detail_order for w in template.warehouses}
    else:
        stacks = [list(report.groups[i::3]) for i in range(3)]
        rank = {}
    gap = max(24, round(width * .068))
    table_width = (width - 2 * gap - 2) // 3
    widths = [int(table_width * v) for v in (.485, .165, .175)]
    widths.append(table_width - sum(widths))
    font = _get_font(ImageFont, 18, bold=False, font_path=font_path)
    bold = _get_font(ImageFont, 18, bold=True, font_path=font_path)
    heading = _get_font(ImageFont, 21, bold=True, font_path=font_path)
    small = _get_font(ImageFont, 15, bold=False, font_path=font_path)
    probe = ImageDraw.Draw(Image.new("RGB", (width, 50), "white"))
    def fit(text, cell_width, heavy=False):
        face = bold if heavy else font
        for size in range(18, 9, -1):
            face = _get_font(ImageFont, size, bold=heavy, font_path=font_path)
            if _text_size(probe, text, face)[0] <= cell_width - 8:
                return text, face
        return _wrap_text(probe, text, face, cell_width - 8), face
    def cells(row, days, total=False):
        return ("" if total else row.warehouse_name,
                format_quantity(row.available_ea),
                format(row.average_daily_outbound(days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
                format_days(row.turnover_days_for(days)))
    def table_spec(label, rows, days, total=None):
        raw = [(label, "实时库存量", f"近{days}天动销", "平均周转天数")]
        raw += [cells(r, days) for r in rows]
        if total is not None:
            raw.append(cells(total, days, True))
        result = []
        for index, values in enumerate(raw):
            heavy = index == 0 or (total is not None and index == len(raw) - 1)
            fitted = [fit(text, widths[i], heavy) for i, text in enumerate(values)]
            row_height = max(26, max(_text_size(probe, text, face)[1] + 8 for text, face in fitted))
            result.append((fitted, row_height, index == 0))
        return result
    summary = table_spec("归属中心仓", center.center_rows, center.window_days, center.center_total)
    tables = [[table_spec(g.center.warehouse_name, sorted(g.details, key=lambda r: rank.get(r.wms_code, 0)), report.window_days)
               for g in stack] for stack in stacks]
    def height(spec):
        return sum(h for _, h, _ in spec)
    title_height, section_gap, group_gap = 30, 28, 76
    detail_title_y = title_height + height(summary) + section_gap
    detail_top = detail_title_y + title_height
    body_bottom = detail_top + max(sum(height(t) for t in stack) + max(0, len(stack)-1) * group_gap for stack in tables)
    note = (f"中心：{center.window_start:%Y-%m-%d} 至 {center.window_end_exclusive:%Y-%m-%d}；"
            f"分仓：{report.window_start:%Y-%m-%d} 至 {report.window_end_exclusive:%Y-%m-%d}（均不含结束日，Asia/Shanghai）\n"
            "库存：可用成品库存加在途（EA）；动销：社区平台订单请求量（创建日减一天）＋其余渠道实发量的日均值（EA/日）；周转：天。显示两位小数，按原始精度计算。")
    if quality_status == "passed_with_warning":
        note += "\n数据提示：存在已按口径处理的源数据异常，请同时查看结果表中的异常说明。"
    note = _wrap_text(probe, note, small, width-16)
    note_height = _text_size(probe, note, small)[1] + 24
    image = Image.new("RGB", (width, body_bottom + note_height), "white")
    draw = ImageDraw.Draw(image)
    def table(x, y, spec):
        for fitted, h, header in spec:
            left = x
            for i, (text, face) in enumerate(fitted):
                box = (left, y, left+widths[i], y+h)
                draw.rectangle(box, fill="#FFC000" if header else "white", outline="#404040", width=1)
                _draw_center(draw, box, text, face, "#111111")
                left += widths[i]
            y += h
        return y
    _draw_left(draw, (0, 0, width, title_height), "1、中心仓周转情况：", heading, "#E60012")
    table(0, title_height, summary)
    _draw_left(draw, (0, detail_title_y, width, detail_top), "2、各分仓周转情况：", heading, "#E60012")
    for index, stack in enumerate(tables):
        y = detail_top
        for spec in stack:
            y = table(index*(table_width+gap), y, spec) + group_gap
    _draw_left(draw, (0, body_bottom+8, width, body_bottom+note_height), note, small, "#505050")
    return image
