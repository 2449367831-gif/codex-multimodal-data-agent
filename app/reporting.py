"""Deterministic business reports built from screened live query results.

The model may choose the query and explain the business meaning, but it never
supplies report numbers.  This module accepts only the public, already screened
result tables stored in a completed chat receipt and derives the report from
those values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import csv
from io import BytesIO, StringIO
import json
import re
from typing import Any, Callable, Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile

from .public_table_text import PUBLIC_TABLE_UNSAFE_TEXT


MAX_REPORT_TABLES = 8
MAX_REPORT_ROWS = 100_000
MAX_REPORT_MODEL_ROWS = 2_000
MAX_REPORT_MODEL_CONTEXT_BYTES = 180_000
MAX_REPORT_TITLE = 160
MAX_REPORT_AUDIENCE = 120
SUPPORTED_REPORT_FORMATS = ("html", "pdf", "word", "excel", "csv", "png")


class ReportSourceError(ValueError):
    """A report cannot be generated from the supplied query receipt."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ReportFormatError(ValueError):
    """A requested output format is unavailable or cannot be rendered."""


@dataclass(frozen=True, slots=True)
class ReportTable:
    title: str
    caption: str
    columns: tuple[dict[str, Any], ...]
    rows: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]
    summary: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ReportDocument:
    title: str
    audience: str
    generated_at: str
    tables: tuple[ReportTable, ...]
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    warnings: tuple[str, ...]
    source_request_id: str
    model_summary: str = ""
    model_analysis: tuple[str, ...] = ()
    model_recommendations: tuple[str, ...] = ()


def _text(value: Any, *, name: str, maximum: int, default: str = "") -> str:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise ReportSourceError("invalid_report_source", f"{name} exceeds the report limit")
    if name in {"title", "audience"} and PUBLIC_TABLE_UNSAFE_TEXT.search(result):
        raise ReportSourceError("invalid_report_source", f"{name} contains private query text")
    return result or default


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _format_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{value:.0f}"
    return format(value.quantize(Decimal("0.01")), "f")


def _html(value: Any) -> str:
    text = str(value if value is not None else "")
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _filename(value: str) -> str:
    clean = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value, flags=re.UNICODE).strip("-.")
    return clean[:80] or "doris-report"


def _model_text(value: Any, *, maximum: int) -> str | None:
    """Keep model-authored prose public, bounded, and free of private details."""

    if not isinstance(value, str):
        return None
    result = re.sub(r"\s+", " ", value).strip()
    if not result or len(result) > maximum or PUBLIC_TABLE_UNSAFE_TEXT.search(result):
        return None
    return result


def normalize_report_tables(
    payload: Mapping[str, Any],
    *,
    page_reader: Callable[[str, int], Mapping[str, Any]] | None = None,
) -> tuple[ReportTable, ...]:
    """Expand complete result pages and validate the receipt's public tables."""

    if not isinstance(payload, Mapping):
        raise ReportSourceError("invalid_report_source", "查询结果不是有效对象")
    outcome = payload.get("outcome")
    if payload.get("status") != "completed" or not isinstance(outcome, Mapping) or outcome.get("state") != "completed":
        raise ReportSourceError("report_source_not_completed", "只有已完成的查询结果可以生成报告")
    reportable_states = {"live_success", "summary_only", "batch_complete"}
    if payload.get("query_status") != "succeeded" or payload.get("result_state") not in reportable_states:
        state = str(payload.get("query_status") or payload.get("result_state") or "unknown")
        if state == "empty_result":
            raise ReportSourceError("empty_result", "查询成功，但当前条件下没有数据，无法生成报告")
        raise ReportSourceError("report_source_not_live", "当前结果不是可确认的实时数据")

    raw_tables = payload.get("business_tables")
    if raw_tables is None:
        raw_table = payload.get("business_table")
        raw_tables = [raw_table] if raw_table is not None else []
    if not isinstance(raw_tables, list) or not 1 <= len(raw_tables) <= MAX_REPORT_TABLES:
        raise ReportSourceError("invalid_report_source", "查询结果缺少可生成报告的表格")

    normalized: list[ReportTable] = []
    total_rows = 0
    for raw in raw_tables:
        if not isinstance(raw, Mapping):
            raise ReportSourceError("invalid_report_source", "查询结果表格格式不完整")
        title = _text(raw.get("title"), name="title", maximum=MAX_REPORT_TITLE, default="查询结果")
        caption = _text(raw.get("caption"), name="caption", maximum=500)
        columns = raw.get("columns")
        if not isinstance(columns, list) or not columns or len(columns) > 32:
            raise ReportSourceError("invalid_report_source", "查询结果缺少有效列定义")
        clean_columns: list[dict[str, Any]] = []
        keys: set[str] = set()
        for column in columns:
            if not isinstance(column, Mapping):
                raise ReportSourceError("invalid_report_source", "查询结果列定义无效")
            key = _text(column.get("key"), name="column key", maximum=100)
            label = _text(column.get("label"), name="column label", maximum=80, default=key)
            if not key or key in keys:
                raise ReportSourceError("invalid_report_source", "查询结果列定义重复")
            keys.add(key)
            clean_columns.append({
                "key": key,
                "label": label,
                "kind": str(column.get("kind") or "text"),
                "unit": _text(column.get("unit"), name="column unit", maximum=40),
            })
        rows = raw.get("rows")
        if not isinstance(rows, list):
            raise ReportSourceError("invalid_report_source", "查询结果缺少行数据")
        pagination = raw.get("pagination")
        expected = len(rows)
        if pagination is not None:
            if not isinstance(pagination, Mapping) or type(pagination.get("total_rows")) is not int:
                raise ReportSourceError("invalid_report_source", "结果分页信息无效")
            expected = int(pagination["total_rows"])
            result_id = str(pagination.get("result_id") or "")
            page_count = pagination.get("page_count")
            if expected < 0 or expected > MAX_REPORT_ROWS or not result_id or type(page_count) is not int:
                raise ReportSourceError("invalid_report_source", "结果分页范围无效")
            if expected != len(rows):
                if page_reader is None:
                    raise ReportSourceError("report_source_incomplete", "结果尚未完整取回，无法生成完整报告")
                expanded: list[dict[str, Any]] = []
                for page_number in range(1, page_count + 1):
                    page = page_reader(result_id, page_number)
                    if not isinstance(page, Mapping) or not isinstance(page.get("rows"), list):
                        raise ReportSourceError("report_source_incomplete", "结果分页读取失败，无法生成完整报告")
                    expanded.extend(page["rows"])
                rows = expanded
            if len(rows) != expected:
                raise ReportSourceError("report_source_incomplete", "结果行数不完整，无法生成报告")
        if len(rows) > MAX_REPORT_ROWS:
            raise ReportSourceError("report_source_too_large", "结果超过报告行数上限，请先汇总或缩小范围")
        clean_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ReportSourceError("invalid_report_source", "查询结果行格式无效")
            clean_rows.append({key: row.get(key) for key in keys})
        notes = tuple(_text(note, name="note", maximum=600) for note in (raw.get("notes") or []) if note)
        summary = tuple(dict(item) for item in (raw.get("summary") or []) if isinstance(item, Mapping))
        normalized.append(ReportTable(title, caption, tuple(clean_columns), tuple(clean_rows), notes, summary))
        total_rows += len(clean_rows)
    if total_rows == 0:
        raise ReportSourceError("empty_result", "查询成功，但当前条件下没有数据，无法生成报告")
    return tuple(normalized)


def report_model_context(
    document: ReportDocument,
    *,
    report_request: str = "",
) -> dict[str, Any]:
    """Build a bounded, exact-data context for the report-writing model.

    The complete result remains host-owned and is used for files. The model
    receives the verified table schema, bounded rows, and host-computed totals
    so it can write useful business prose without becoming the source of truth.
    """

    tables: list[dict[str, Any]] = []
    for table in document.tables:
        numeric_totals: list[dict[str, str]] = []
        for column in table.columns:
            values = [_number(row.get(column["key"])) for row in table.rows]
            values = [value for value in values if value is not None]
            if values and (column["kind"] == "number" or len(values) == len(table.rows)):
                numeric_totals.append({
                    "label": column["label"],
                    "unit": column.get("unit", ""),
                    "sum": _format_decimal(sum(values)),
                })
        tables.append({
            "title": table.title,
            "caption": table.caption,
            "columns": list(table.columns),
            "row_count": len(table.rows),
            "numeric_totals": numeric_totals,
            "notes": list(table.notes),
            "rows": [dict(row) for row in table.rows[:MAX_REPORT_MODEL_ROWS]],
            "rows_truncated_for_model": len(table.rows) > MAX_REPORT_MODEL_ROWS,
        })
    context = {
        "title": document.title,
        "audience": document.audience,
        "report_request": _model_text(report_request, maximum=2_000) or "根据查询结果生成供应链业务报告",
        "verified_findings": list(document.findings),
        "verified_warnings": list(document.warnings),
        "tables": tables,
    }
    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_REPORT_MODEL_CONTEXT_BYTES:
        return context
    # Keep all table definitions and host totals, then remove only row samples
    # until the model context is inside the fixed gateway budget.
    for table in tables:
        table["rows"] = []
        table["rows_truncated_for_model"] = True
    return context


def apply_model_narrative(document: ReportDocument, narrative: Any) -> ReportDocument:
    """Attach screened model prose while retaining host-owned numbers and tables."""

    if not isinstance(narrative, Mapping):
        return document
    summary = _model_text(narrative.get("summary"), maximum=1_200) or ""
    analysis = tuple(
        item for item in (
            _model_text(value, maximum=800)
            for value in (narrative.get("analysis") or narrative.get("key_findings") or [])
        ) if item
    )[:8]
    recommendations = tuple(
        item for item in (
            _model_text(value, maximum=800)
            for value in (narrative.get("recommendations") or [])
        ) if item
    )[:8]
    if not summary and not analysis and not recommendations:
        return document
    return ReportDocument(
        title=document.title,
        audience=document.audience,
        generated_at=document.generated_at,
        tables=document.tables,
        findings=document.findings,
        recommendations=document.recommendations,
        warnings=document.warnings,
        source_request_id=document.source_request_id,
        model_summary=summary,
        model_analysis=analysis,
        model_recommendations=recommendations,
    )


def build_report_document(
    payload: Mapping[str, Any],
    tables: Sequence[ReportTable],
    *,
    title: str | None = None,
    audience: str = "供应链业务",
    generated_at: str = "",
    narrative: Mapping[str, Any] | None = None,
) -> ReportDocument:
    if not tables:
        raise ReportSourceError("empty_result", "没有可用于报告的数据")
    report_title = _text(title, name="title", maximum=MAX_REPORT_TITLE, default="Doris 查询数据报告")
    report_audience = _text(audience, name="audience", maximum=MAX_REPORT_AUDIENCE, default="供应链业务")
    findings: list[str] = []
    recommendations: list[str] = []
    warnings: list[str] = []
    total_rows = sum(len(table.rows) for table in tables)
    findings.append(f"本次报告包含 {len(tables)} 张结果表，共 {total_rows} 行已校验数据。")
    result_state = str(payload.get("result_state") or "")
    if result_state == "summary_only":
        warnings.append("本报告基于已校验的范围汇总，原始明细尚未全部取回。")
    elif result_state == "batch_complete":
        warnings.append("本报告基于已取回并校验的明细批次，未取回批次不纳入本报告。")
    for table in tables:
        numeric = []
        for column in table.columns:
            values = [_number(row.get(column["key"])) for row in table.rows]
            values = [value for value in values if value is not None]
            if values and (column["kind"] == "number" or len(values) == len(table.rows)):
                numeric.append((column["label"], column.get("unit", ""), sum(values)))
        if numeric:
            headline = "；".join(
                f"{label}合计 {_format_decimal(value)}{unit}"
                for label, unit, value in numeric[:3]
            )
            findings.append(f"{table.title}：{headline}。")
        if any(row.get(column["key"]) is None for row in table.rows for column in table.columns):
            warnings.append(f"{table.title}存在空值，报告保留原始缺失状态。")
        if table.notes:
            warnings.extend(f"{table.title}：{note}" for note in table.notes[:3])
    recommendations.append("结合明细表、口径说明和异常提示复核业务结论；报告中的数值均来自本次已校验查询结果。")
    source_request_id = _text(payload.get("request_id"), name="request_id", maximum=200, default="current-query")
    document = ReportDocument(
        title=report_title,
        audience=report_audience,
        generated_at=generated_at,
        tables=tuple(tables),
        findings=tuple(findings[:20]),
        recommendations=tuple(recommendations),
        warnings=tuple(dict.fromkeys(warnings))[:20],
        source_request_id=source_request_id,
    )
    return apply_model_narrative(document, narrative)


def render_html(document: ReportDocument) -> bytes:
    table_html = []
    for table in document.tables:
        headers = "".join(
            f"<th>{_html(column['label'])}{f'（{_html(column['unit'])}）' if column.get('unit') else ''}</th>"
            for column in table.columns
        )
        body = "".join(
            "<tr>" + "".join(f"<td>{_html(row.get(column['key']))}</td>" for column in table.columns) + "</tr>"
            for row in table.rows
        )
        table_html.append(
            f"<section><h2>{_html(table.title)}</h2><p class='caption'>{_html(table.caption)}</p>"
            f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></section>"
        )
    def list_html(items: Sequence[str]) -> str:
        return "".join(f"<li>{_html(item)}</li>" for item in items) or "<li>无</li>"
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{_html(document.title)}</title>
<style>
@page {{ size: A4 landscape; margin: 14mm; }}
body {{ color:#18232d; font-family:"Microsoft YaHei",Arial,sans-serif; line-height:1.55; margin:0; background:#f4f6f8; }}
main {{ max-width:1180px; margin:0 auto; padding:38px 28px; background:white; }}
h1 {{ margin:0 0 4px; font-size:26px; }} h2 {{ margin:28px 0 8px; font-size:18px; border-left:4px solid #d89a00; padding-left:10px; }}
h3 {{ margin:18px 0 6px; font-size:15px; }} .meta,.caption {{ color:#66727e; font-size:12px; }}
.lead {{ margin:18px 0; padding:14px 16px; background:#fff5d6; border-left:4px solid #d89a00; }}
ul {{ margin-top:8px; }} table {{ border-collapse:collapse; width:100%; margin:12px 0 24px; font-size:12px; }}
th,td {{ border:1px solid #cbd3d9; padding:7px 8px; text-align:left; vertical-align:top; }} th {{ background:#fff2bd; }}
tbody tr:nth-child(even) {{ background:#fafbfc; }} .footer {{ border-top:1px solid #dce2e7; color:#7a858f; font-size:11px; margin-top:30px; padding-top:10px; }}
</style></head><body><main>
<header><h1>{_html(document.title)}</h1><div class="meta">面向：{_html(document.audience)} · 生成时间：{_html(document.generated_at or '本次查询')}</div></header>
<div class="lead"><strong>核心结论</strong>{f'<p>{_html(document.model_summary)}</p>' if document.model_summary else ''}<ul>{list_html(document.findings)}</ul></div>
<section><h2>背景与目标</h2><p>本报告基于本次已完成的实时查询结果，按业务人员的查看习惯整理为可核验的数据表和结论。</p></section>
<section><h2>数据概览</h2><p>共 {len(document.tables)} 张结果表、{sum(len(t.rows) for t in document.tables)} 行；结果未使用模拟数据。</p></section>
<section><h2>深入分析</h2>{''.join(table_html)}</section>
<section><h2>深入解读</h2><ul>{list_html(document.model_analysis or document.findings[1:])}</ul></section>
<section><h2>结论与建议</h2><ul>{list_html(document.model_recommendations or document.recommendations)}</ul></section>
  <section><h2>数据处理说明</h2><ul><li>报告只接受查询状态为成功且已通过宿主校验的实时结果；分批结果会明确标注当前取回范围。</li>{list_html(document.warnings)}</ul></section>
<div class="footer">来源请求标识：{_html(document.source_request_id)}。本报告不包含 SQL、凭据或内部执行日志。</div>
</main></body></html>"""
    return html.encode("utf-8")


def render_csv(document: ReportDocument) -> bytes:
    outputs: list[tuple[str, bytes]] = []
    for index, table in enumerate(document.tables, 1):
        stream = StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow([f"{column['label']}{f'（{column['unit']}）' if column.get('unit') else ''}" for column in table.columns])
        for row in table.rows:
            writer.writerow(["" if row.get(column["key"]) is None else row.get(column["key"]) for column in table.columns])
        outputs.append((f"{index:02d}-{_filename(table.title)}.csv", "\ufeff".encode("utf-8") + stream.getvalue().encode("utf-8")))
    if len(outputs) == 1:
        return outputs[0][1]
    result = BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED) as archive:
        for name, content in outputs:
            archive.writestr(name, content)
    return result.getvalue()


def render_excel(document: ReportDocument) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError as exc:
        raise ReportFormatError("Excel 输出依赖未安装") from exc
    workbook = Workbook()
    workbook.remove(workbook.active)
    for index, table in enumerate(document.tables, 1):
        sheet = workbook.create_sheet((f"{index:02d}-{table.title}"[:31]) or f"Sheet{index}")
        sheet.append([column["label"] + (f"（{column['unit']}）" if column.get("unit") else "") for column in table.columns])
        for row in table.rows:
            sheet.append([row.get(column["key"]) for column in table.columns])
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="7A4A00")
            cell.fill = PatternFill("solid", fgColor="FFF2BD")
            cell.alignment = Alignment(horizontal="center")
        sheet.freeze_panes = "A2"
        for column in sheet.columns:
            width = min(42, max(10, max(len(str(cell.value or "")) for cell in column) + 2))
            sheet.column_dimensions[column[0].column_letter].width = width
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def render_word(document: ReportDocument) -> bytes:
    try:
        from docx import Document
    except ImportError as exc:
        raise ReportFormatError("Word 输出依赖未安装") from exc
    word = Document()
    word.add_heading(document.title, 0)
    word.add_paragraph(f"面向：{document.audience} · 生成时间：{document.generated_at or '本次查询'}")
    word.add_heading("核心结论", level=1)
    for item in document.findings:
        word.add_paragraph(item, style="List Bullet")
    if document.model_summary:
        word.add_heading("模型解读", level=1)
        word.add_paragraph(document.model_summary)
    word.add_heading("背景与目标", level=1)
    word.add_paragraph("本报告基于本次已完成的实时查询结果，按业务人员的查看习惯整理。");
    word.add_heading("数据概览", level=1)
    word.add_paragraph(f"共 {len(document.tables)} 张结果表、{sum(len(t.rows) for t in document.tables)} 行。")
    word.add_heading("深入分析", level=1)
    for table in document.tables:
        word.add_heading(table.title, level=2)
        if table.caption:
            word.add_paragraph(table.caption)
        grid = word.add_table(rows=1, cols=len(table.columns))
        grid.style = "Table Grid"
        for cell, column in zip(grid.rows[0].cells, table.columns, strict=True):
            cell.text = column["label"] + (f"（{column['unit']}）" if column.get("unit") else "")
        for row in table.rows:
            cells = grid.add_row().cells
            for cell, column in zip(cells, table.columns, strict=True):
                cell.text = "" if row.get(column["key"]) is None else str(row.get(column["key"]))
    word.add_heading("深入解读", level=1)
    for item in document.model_analysis or document.findings[1:]:
        word.add_paragraph(item, style="List Bullet")
    word.add_heading("结论与建议", level=1)
    for item in document.model_recommendations or document.recommendations:
        word.add_paragraph(item, style="List Bullet")
    word.add_heading("数据处理说明", level=1)
    for item in document.warnings or ("结果未使用模拟数据，未发现需要额外提示的缺失项。",):
        word.add_paragraph(item, style="List Bullet")
    output = BytesIO()
    word.save(output)
    return output.getvalue()


def render_pdf(document: ReportDocument) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFError, TTFont
    except ImportError as exc:
        raise ReportFormatError("PDF 输出依赖未安装") from exc
    output = BytesIO()
    styles = getSampleStyleSheet()
    font_name = "Helvetica"
    font_bold = "Helvetica-Bold"
    for name, path in (("DorisReportCN", "C:/Windows/Fonts/msyh.ttc"), ("DorisReportCNBold", "C:/Windows/Fonts/msyhbd.ttc")):
        try:
            pdfmetrics.registerFont(TTFont(name, path))
        except (OSError, TTFError):
            continue
    if "DorisReportCN" in pdfmetrics.getRegisteredFontNames():
        font_name, font_bold = "DorisReportCN", "DorisReportCNBold" if "DorisReportCNBold" in pdfmetrics.getRegisteredFontNames() else "DorisReportCN"
    styles.add(ParagraphStyle(name="CnBody", parent=styles["BodyText"], fontName=font_name, fontSize=8, leading=11))
    styles.add(ParagraphStyle(name="CnHeading", parent=styles["Heading2"], fontName=font_bold, fontSize=13, leading=16, spaceBefore=10))
    styles["Title"].fontName = font_bold
    story: list[Any] = [Paragraph(_html(document.title), styles["Title"]), Paragraph(_html(f"面向：{document.audience} · 生成时间：{document.generated_at or '本次查询'}"), styles["CnBody"]), Spacer(1, 6)]
    story.append(Paragraph("核心结论", styles["CnHeading"]))
    if document.model_summary:
        story.append(Paragraph(_html(document.model_summary), styles["CnBody"]))
    for item in document.findings:
        story.append(Paragraph("• " + _html(item), styles["CnBody"]))
    story.append(Paragraph("数据概览", styles["CnHeading"]))
    story.append(Paragraph(_html(f"共 {len(document.tables)} 张结果表、{sum(len(t.rows) for t in document.tables)} 行。"), styles["CnBody"]))
    for table in document.tables:
        story.append(Paragraph(_html(table.title), styles["CnHeading"]))
        data = [[Paragraph(_html(column["label"]), styles["CnBody"]) for column in table.columns]]
        data.extend([[Paragraph(_html(row.get(column["key"])), styles["CnBody"]) for column in table.columns] for row in table.rows])
        grid = Table(data, repeatRows=1)
        grid.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FFF2BD")), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BFC8CF")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 7)]))
        story.extend([grid, Spacer(1, 8)])
    story.append(Paragraph("深入解读", styles["CnHeading"]))
    for item in document.model_analysis or document.findings[1:]:
        story.append(Paragraph("• " + _html(item), styles["CnBody"]))
    story.append(Paragraph("结论与建议", styles["CnHeading"]))
    for item in document.model_recommendations or document.recommendations:
        story.append(Paragraph("• " + _html(item), styles["CnBody"]))
    SimpleDocTemplate(output, pagesize=landscape(A4), rightMargin=12 * mm, leftMargin=12 * mm, topMargin=10 * mm, bottomMargin=10 * mm).build(story)
    return output.getvalue()


def render_png(document: ReportDocument) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ReportFormatError("PNG 输出依赖未安装") from exc
    font_candidates = ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf")
    font_path = next((candidate for candidate in font_candidates if __import__('os').path.exists(candidate)), None)
    font = ImageFont.truetype(font_path, 22) if font_path else ImageFont.load_default()
    small = ImageFont.truetype(font_path, 15) if font_path else ImageFont.load_default()
    lines = [document.title, f"面向：{document.audience}", "", "核心结论"]
    if document.model_summary:
        lines.append(document.model_summary)
    lines += [f"• {item}" for item in document.findings]
    lines += ["", "深入解读"] + [f"• {item}" for item in document.model_analysis or document.findings[1:]]
    lines += ["", "结论与建议"] + [f"• {item}" for item in document.model_recommendations or document.recommendations]
    for table in document.tables:
        lines.extend(["", table.title, " | ".join(column["label"] for column in table.columns)])
        lines.extend(" | ".join(str(row.get(column["key"]) or "") for column in table.columns) for row in table.rows[:80])
    width = 1800
    height = max(500, 42 * len(lines) + 50)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y = 25
    for index, line in enumerate(lines):
        draw.text((30, y), line[:190], fill="#18232d", font=font if index == 0 else small)
        y += 42 if index == 0 else 27
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


RENDERERS = {
    "html": (render_html, "text/html; charset=utf-8", ".html"),
    "pdf": (render_pdf, "application/pdf", ".pdf"),
    "word": (render_word, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "excel": (render_excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "csv": (render_csv, "text/csv; charset=utf-8", ".csv"),
    "png": (render_png, "image/png", ".png"),
}


def render_format(document: ReportDocument, format_name: str) -> tuple[bytes, str, str]:
    name = str(format_name or "").strip().casefold()
    if name not in RENDERERS:
        raise ReportFormatError(f"不支持的报告格式：{name}")
    renderer, content_type, suffix = RENDERERS[name]
    if name == "csv" and len(document.tables) > 1:
        content_type, suffix = "application/zip", ".zip"
    return renderer(document), content_type, suffix


__all__ = [
    "MAX_REPORT_ROWS", "RENDERERS", "ReportDocument", "ReportFormatError", "ReportSourceError", "ReportTable",
    "SUPPORTED_REPORT_FORMATS", "build_report_document", "normalize_report_tables", "render_format",
]
