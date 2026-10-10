(() => {
  "use strict";

  const MAX_EXPORT_ROWS = 100000;
  const MAX_PNG_ROWS = 500;

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function safeCell(value) {
    let text = value == null ? "" : String(value);
    if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return text;
  }

  function incrementDecimalDigits(value) {
    const digits = value.split("");
    let carry = 1;
    for (let index = digits.length - 1; index >= 0; index -= 1) {
      const next = Number(digits[index]) + carry;
      digits[index] = String(next % 10);
      carry = next > 9 ? 1 : 0;
    }
    return `${carry ? "1" : ""}${digits.join("")}`;
  }

  function formatNumber(value) {
    const text = String(value ?? "").trim();
    if (!/^-?\d+(?:\.\d+)?$/.test(text)) return text;
    let unsigned = text;
    let sign = "";
    if (unsigned.startsWith("-")) {
      sign = "-";
      unsigned = unsigned.slice(1);
    }
    let [integer, fraction = ""] = unsigned.split(".");
    integer = integer.replace(/^0+(?=\d)/, "");
    let cents = `${fraction}00`.slice(0, 2);
    if ((fraction[2] || "0") >= "5") {
      const digits = `${cents}0`.split("");
      let carry = 1;
      for (let index = 1; index >= 0; index -= 1) {
        const next = Number(digits[index]) + carry;
        digits[index] = String(next % 10);
        carry = next > 9 ? 1 : 0;
      }
      cents = digits.slice(0, 2).join("");
      if (carry) integer = incrementDecimalDigits(integer);
    }
    const normalizedSign = sign && !(integer === "0" && cents === "00") ? sign : "";
    return `${normalizedSign}${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${cents}`;
  }

  function displayCell(value, column) {
    return value == null ? "暂无" : column?.kind === "number" ? formatNumber(value) : String(value);
  }

  function fileName(value, extension) {
    const cleaned = String(value || "查询结果")
      .replace(/[\\/:*?"<>|\x00-\x1f]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80) || "查询结果";
    return `${cleaned}.${extension}`;
  }

  function downloadBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function columnLabel(column) {
    return `${column.label || column.key}${column.unit ? `（${column.unit}）` : ""}`;
  }

  function rowsToMatrix(table, rows) {
    return [
      table.columns.map(columnLabel),
      ...rows.map(row => table.columns.map(column => safeCell(displayCell(row[column.key], column)))),
    ];
  }

  function csvText(table, rows) {
    const cell = value => `"${String(value ?? "").replace(/"/g, '""')}"`;
    return rowsToMatrix(table, rows).map(row => row.map(cell).join(",")).join("\r\n");
  }

  function reportIntro(table) {
    const summary = (table.summary || []).map(item =>
      `<span><strong>${escapeHtml(item.label)}</strong> ${escapeHtml(formatNumber(item.value))}${item.unit ? ` ${escapeHtml(item.unit)}` : ""}</span>`
    ).join(" · ");
    const notes = (table.notes || []).map(note => `<li>${escapeHtml(note)}</li>`).join("");
    return `<h1>${escapeHtml(table.title || "查询结果")}</h1>` +
      `${table.caption ? `<p class="caption">${escapeHtml(table.caption)}</p>` : ""}` +
      `${summary ? `<p class="summary">${summary}</p>` : ""}` +
      `${notes ? `<ul>${notes}</ul>` : ""}`;
  }

  function tableHtml(table, rows) {
    const head = table.columns.map(column => `<th>${escapeHtml(columnLabel(column))}</th>`).join("");
    const body = rows.map(row => `<tr>${table.columns.map(column => `<td>${escapeHtml(safeCell(displayCell(row[column.key], column)))}</td>`).join("")}</tr>`).join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function reportHtml(table, rows, mode) {
    const title = mode === "word" ? "Word" : "Excel";
    return `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(table.title || title)}</title><style>
      body{font-family:"Microsoft YaHei",Arial,sans-serif;color:#17202a;margin:28px;font-size:12px}
      h1{font-size:20px;margin:0 0 8px} .caption{color:#5d6873;margin:0 0 12px}
      .summary{padding:8px 10px;background:#fff4cf;border-left:3px solid #fabe00}
      table{border-collapse:collapse;width:100%;margin-top:16px} th,td{border:1px solid #c5cdd4;padding:7px 8px;text-align:left;vertical-align:top}
      th{background:#fff4cf;font-weight:700} td{mso-number-format:"\\@"} li{margin:4px 0}
    </style></head><body>${reportIntro(table)}${tableHtml(table, rows)}</body></html>`;
  }

  function truncateText(value, max) {
    const text = String(value ?? "");
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
  }

  function svgText(value, x, y, size, weight = "400", color = "#17202a") {
    return `<text x="${x}" y="${y}" font-family="Microsoft YaHei,Arial,sans-serif" font-size="${size}" font-weight="${weight}" fill="${color}">${escapeHtml(value)}</text>`;
  }

  function pngSvg(table, rows) {
    const columns = table.columns;
    const widths = columns.map(column => Math.max(112, Math.min(300, 24 + Math.max(String(columnLabel(column)).length * 14, ...rows.slice(0, 100).map(row => String(displayCell(row[column.key], column)).length * 12)))));
    const width = widths.reduce((sum, value) => sum + value, 0);
    const rowHeight = 32;
    const headerHeight = 54;
    const introHeight = table.caption ? 86 : 64;
    const height = introHeight + headerHeight + rowHeight * rows.length + 24;
    let x = 0;
    let grid = "";
    columns.forEach((column, index) => {
      grid += `<rect x="${x}" y="${introHeight}" width="${widths[index]}" height="${headerHeight}" fill="#fff4cf" stroke="#c5cdd4"/>`;
      grid += svgText(truncateText(columnLabel(column), 24), x + 10, introHeight + 33, 16, "700", "#632c16");
      x += widths[index];
    });
    rows.forEach((row, rowIndex) => {
      let cellX = 0;
      columns.forEach((column, columnIndex) => {
        const fill = rowIndex % 2 ? "#f8fafb" : "#ffffff";
        grid += `<rect x="${cellX}" y="${introHeight + headerHeight + rowIndex * rowHeight}" width="${widths[columnIndex]}" height="${rowHeight}" fill="${fill}" stroke="#dde2e7"/>`;
        grid += svgText(truncateText(displayCell(row[column.key], column), Math.max(8, Math.floor(widths[columnIndex] / 12))), cellX + 10, introHeight + headerHeight + rowIndex * rowHeight + 22, 14);
        cellX += widths[columnIndex];
      });
    });
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#ffffff"/>${svgText(truncateText(table.title || "查询结果", 70), 18, 30, 22, "700")}${table.caption ? svgText(truncateText(table.caption, 120), 18, 52, 13, "400", "#5d6873") : ""}${grid}</svg>`;
  }

  async function svgToPng(table, rows, name) {
    const svg = pngSvg(table, rows);
    const blob = new Blob([svg], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    try {
      const image = new Image();
      await new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = reject;
        image.src = url;
      });
      const maxPixels = 40000000;
      const scale = Math.min(2, 7000 / image.width, 14000 / image.height, Math.sqrt(maxPixels / (image.width * image.height)));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.floor(image.width * scale));
      canvas.height = Math.max(1, Math.floor(image.height * scale));
      const context = canvas.getContext("2d");
      context.scale(scale, scale);
      context.drawImage(image, 0, 0);
      const png = await new Promise((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error("PNG生成失败")), "image/png"));
      downloadBlob(png, name);
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  function printPdf(table, rows, popup) {
    if (!popup || popup.closed) throw new Error("浏览器阻止了打印窗口，请允许弹出窗口后重试");
    popup.document.open();
    popup.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(table.title || "查询结果")}</title><style>@page{size:auto;margin:14mm}body{font-family:"Microsoft YaHei",Arial,sans-serif;color:#17202a;font-size:10pt}h1{font-size:18pt;margin:0 0 6pt}.caption{color:#5d6873;margin:0 0 10pt}.summary{padding:6pt;background:#fff4cf;border-left:3pt solid #fabe00}table{border-collapse:collapse;width:100%;margin-top:10pt;page-break-inside:auto}thead{display:table-header-group}tr{page-break-inside:avoid}th,td{border:1px solid #c5cdd4;padding:5pt;text-align:left;vertical-align:top}th{background:#fff4cf}</style></head><body>${reportIntro(table)}${tableHtml(table, rows)}</body></html>`);
    popup.document.close();
    popup.focus();
    popup.addEventListener("afterprint", () => popup.close(), { once: true });
    window.setTimeout(() => popup.print(), 250);
  }

  function create({ loadPage, notify = () => {} } = {}) {
    async function collect(table) {
      if (!table || !Array.isArray(table.columns) || !Array.isArray(table.rows)) throw new Error("结果表结构不完整，无法导出");
      const page = table.pagination;
      const total = page?.total_rows || table.rows.length;
      if (!Number.isSafeInteger(total) || total < 0 || total > MAX_EXPORT_ROWS) throw new Error(`结果超过导出上限（${MAX_EXPORT_ROWS}行），请缩小查询范围`);
      const rows = [];
      const pageCount = page?.page_count || 1;
      for (let pageNumber = 1; pageNumber <= pageCount; pageNumber += 1) {
        const part = page && pageNumber === page.page ? table : page ? await loadPage(page.result_id, pageNumber) : table;
        if (!part || JSON.stringify(part.columns) !== JSON.stringify(table.columns) || !Array.isArray(part.rows)) throw new Error("结果分页校验失败，未生成文件");
        if (page && (!part.pagination || part.pagination.result_id !== page.result_id || part.pagination.page !== pageNumber || part.pagination.total_rows !== total)) throw new Error("结果分页不连续，未生成文件");
        rows.push(...part.rows);
      }
      if (rows.length !== total) throw new Error(`结果完整性校验失败：已取回${rows.length}行，应为${total}行`);
      return rows;
    }

    async function exportTable(table, format) {
      const formats = new Set(["excel", "word", "csv", "png", "pdf"]);
      if (!formats.has(format)) return;
      const printWindow = format === "pdf" ? window.open("", "_blank", "noopener,noreferrer") : null;
      if (format === "pdf" && !printWindow) {
        notify("浏览器阻止了打印窗口，请允许弹出窗口后重试");
        return;
      }
      let rows;
      try {
        rows = await collect(table);
        if (format === "png" && rows.length > MAX_PNG_ROWS) throw new Error(`PNG最多导出${MAX_PNG_ROWS}行；当前${rows.length}行，请使用Excel、Word、CSV或PDF导出完整结果`);
        const baseName = fileName(table.title, format === "excel" ? "xls" : format === "word" ? "doc" : format);
        if (format === "csv") downloadBlob(new Blob(["\uFEFF", csvText(table, rows)], { type: "text/csv;charset=utf-8" }), baseName);
        else if (format === "excel") downloadBlob(new Blob([reportHtml(table, rows, "excel")], { type: "application/vnd.ms-excel;charset=utf-8" }), baseName);
        else if (format === "word") downloadBlob(new Blob([reportHtml(table, rows, "word")], { type: "application/msword;charset=utf-8" }), baseName);
        else if (format === "png") await svgToPng(table, rows, baseName);
        else printPdf(table, rows, printWindow);
        notify(format === "pdf" ? "已打开打印窗口，可选择保存为PDF" : `${format.toUpperCase()}文件已生成`);
      } catch (error) {
        if (printWindow && !printWindow.closed) printWindow.close();
        notify(error?.message || "导出失败，未生成文件");
      }
    }

    return { exportTable };
  }

  globalThis.TableExport = { create };
})();
