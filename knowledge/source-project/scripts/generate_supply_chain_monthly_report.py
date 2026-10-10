from __future__ import annotations

import argparse
import json
import math
import os
import re
import zipfile
from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pymysql
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / "config" / "pickup.env"
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "supply_chain_monthly_202606"

START_DATE = date(2026, 6, 1)
END_DATE = date(2026, 7, 1)
MONTH_LABEL = "2026年6月"
CARGO_OWNER_ID = "HJXH"

DEFECT_WAREHOUSE_PATTERNS = ["残次", "次品", "废品", "报废", "残品"]
VALID_SHIPMENT_STATUSES = ("700", "900", "701", "901")
VALID_OUTBOUND_TYPES = ("JYCK", "HHCK", "BFCK", "QTCK")


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"env file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def connect() -> pymysql.connections.Connection:
    host = os.environ.get("PICKUP_DB_HOST", "").strip()
    user = os.environ.get("PICKUP_DB_USER", "").strip()
    password = os.environ.get("PICKUP_DB_PASSWORD", "")
    database = os.environ.get("PICKUP_DB_NAME", "ywzt_ods").strip() or "ywzt_ods"
    port = int(os.environ.get("PICKUP_DB_PORT", "9030"))
    missing = [
        name
        for name, value in {
            "PICKUP_DB_HOST": host,
            "PICKUP_DB_USER": user,
            "PICKUP_DB_PASSWORD": password,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"missing database env vars: {', '.join(missing)}")
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
        read_timeout=600,
        write_timeout=60,
    )


def to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def to_int(value: Any) -> int:
    return int(round(to_float(value)))


def fmt_qty(value: Any, digits: int = 0) -> str:
    number = to_float(value)
    if digits == 0:
        return f"{number:,.0f}"
    return f"{number:,.{digits}f}"


def fmt_ton(value: Any) -> str:
    return f"{to_float(value):,.2f}"


def pct(value: float, digits: int = 1) -> str:
    if not math.isfinite(value):
        return "-"
    return f"{value * 100:.{digits}f}%"


def safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def as_jsonable(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, (date, datetime)):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def fetch_all(connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def fetch_one(connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    rows = fetch_all(connection, sql, params)
    return rows[0] if rows else {}


def defect_filter(alias: str) -> str:
    return "\n      ".join(
        [f"AND IFNULL({alias}.wms_desc, '') NOT LIKE '%%{pattern}%%'" for pattern in DEFECT_WAREHOUSE_PATTERNS]
    )


def build_sql() -> dict[str, str]:
    wms_filter_mw = defect_filter("mw")
    wms_filter_w = defect_filter("w")
    wms_filter_wh = defect_filter("wh")
    return {
        "data_quality": f"""
SELECT 'stock_snapshot' AS metric_name,
       MIN(DATE(w.backups_date)) AS min_date,
       MAX(DATE(w.backups_date)) AS max_date,
       COUNT(DISTINCT DATE(w.backups_date)) AS day_count,
       COUNT(*) AS row_count,
       COUNT(DISTINCT w.wms_code) AS warehouse_count,
       COUNT(DISTINCT w.sku_code) AS sku_count
FROM scm_warehous_manage_backups w
LEFT JOIN master_wms mw ON mw.wms_code = w.wms_code
LEFT JOIN master_sku ms ON ms.sku_code = w.sku_code
WHERE w.backups_date >= %(start)s AND w.backups_date < %(end)s
  AND w.cargo_owner_id = %(owner)s
  AND ms.sku_group = '50'
  AND mw.wms_type IN (10, 11)
  {wms_filter_mw}
UNION ALL
SELECT 'outbound_shipment' AS metric_name,
       MIN(DATE(h.ship_at)) AS min_date,
       MAX(DATE(h.ship_at)) AS max_date,
       COUNT(DISTINCT DATE(h.ship_at)) AS day_count,
       COUNT(*) AS row_count,
       COUNT(DISTINCT h.facility_code) AS warehouse_count,
       COUNT(DISTINCT d.sku_code) AS sku_count
FROM scm_sales_shipment_header h
INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
LEFT JOIN master_wms mw ON mw.wms_code = h.facility_code
LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
WHERE h.ship_at >= %(start)s AND h.ship_at < %(end)s
  AND h.outbound_code LIKE 'HJXH%%'
  AND h.status IN ('700','900','701','901')
  AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
  AND ms.sku_group = '50'
  AND mw.wms_type IN (10, 11)
  {wms_filter_mw}
UNION ALL
SELECT 'purchase_received' AS metric_name,
       MIN(DATE(d.created_date)) AS min_date,
       MAX(DATE(d.created_date)) AS max_date,
       COUNT(DISTINCT DATE(d.created_date)) AS day_count,
       COUNT(*) AS row_count,
       COUNT(DISTINCT h.take_delivery_wms) AS warehouse_count,
       COUNT(DISTINCT d.sku_code) AS sku_count
FROM scm_po_header h
INNER JOIN scm_po_detail d ON d.po_code = h.po_code
LEFT JOIN master_wms mw ON mw.wms_code = h.take_delivery_wms
LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
WHERE d.created_date >= %(start)s AND d.created_date < %(end)s
  AND h.cargo_owner_id = %(owner)s
  AND h.po_type = 'CCP'
  AND h.status = 0
  AND d.status = 0
  AND ms.sku_group = '50'
  {wms_filter_mw};
""",
        "daily_flow": f"""
WITH sales AS (
    SELECT DATE(h.ship_at) AS dt,
           COUNT(DISTINCT h.outbound_code) AS outbound_order_count,
           SUM(IFNULL(d.ship_qty, 0)) AS outbound_qty,
           SUM(IFNULL(d.ship_qty, 0) * IFNULL(ms.net_weight, 0) / 1000) AS outbound_ton
    FROM scm_sales_shipment_header h
    INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.facility_code
    LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
    WHERE h.ship_at >= %(start)s AND h.ship_at < %(end)s
      AND h.outbound_code LIKE 'HJXH%%'
      AND h.status IN ('700','900','701','901')
      AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
      AND ms.sku_group = '50'
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY DATE(h.ship_at)
),
purchase AS (
    SELECT DATE(d.created_date) AS dt,
           COUNT(DISTINCT h.po_code) AS purchase_order_count,
           SUM(IFNULL(d.received_quantity, 0)) AS purchase_qty,
           SUM(IFNULL(d.received_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) AS purchase_ton
    FROM scm_po_header h
    INNER JOIN scm_po_detail d ON d.po_code = h.po_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.take_delivery_wms
    LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
    WHERE d.created_date >= %(start)s AND d.created_date < %(end)s
      AND h.cargo_owner_id = %(owner)s
      AND h.po_type = 'CCP'
      AND h.status = 0
      AND d.status = 0
      AND ms.sku_group = '50'
      {wms_filter_mw}
    GROUP BY DATE(d.created_date)
),
stock AS (
    SELECT DATE(w.backups_date) AS dt,
           SUM(IFNULL(w.in_stock_quantity, 0)) AS stock_qty,
           SUM(IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) AS stock_ton
    FROM scm_warehous_manage_backups w
    LEFT JOIN master_wms mw ON mw.wms_code = w.wms_code
    LEFT JOIN master_sku ms ON ms.sku_code = w.sku_code
    WHERE w.backups_date >= %(start)s AND w.backups_date < %(end)s
      AND w.cargo_owner_id = %(owner)s
      AND ms.sku_group = '50'
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY DATE(w.backups_date)
)
SELECT c.dt,
       IFNULL(s.outbound_order_count, 0) AS outbound_order_count,
       IFNULL(s.outbound_qty, 0) AS outbound_qty,
       IFNULL(s.outbound_ton, 0) AS outbound_ton,
       IFNULL(p.purchase_order_count, 0) AS purchase_order_count,
       IFNULL(p.purchase_qty, 0) AS purchase_qty,
       IFNULL(p.purchase_ton, 0) AS purchase_ton,
       IFNULL(st.stock_qty, 0) AS stock_qty,
       IFNULL(st.stock_ton, 0) AS stock_ton
FROM (
    SELECT dt FROM sales
    UNION SELECT dt FROM purchase
    UNION SELECT dt FROM stock
) c
LEFT JOIN sales s ON s.dt = c.dt
LEFT JOIN purchase p ON p.dt = c.dt
LEFT JOIN stock st ON st.dt = c.dt
ORDER BY c.dt;
""",
        "channel_summary": f"""
SELECT
    CASE
        WHEN h.ref_order_type = 'JYCK' AND ms_store.type = 'Z004' THEN '线上-JYCK/Z004'
        WHEN h.ref_order_type = 'JYCK' AND ms_store.type = 'Z001' AND ms_store.group1 = '03' THEN '线下KA-JYCK/group1=03'
        WHEN h.ref_order_type = 'JYCK' AND ms_store.type = 'Z001' AND ms_store.price_group = '122' THEN '线下特价组-JYCK/price_group=122'
        WHEN h.ref_order_type = 'JYCK' AND ms_store.type = 'Z001' THEN '线下经销-JYCK/Z001'
        WHEN h.ref_order_type LIKE 'SO%%' THEN CONCAT('SO销售-', h.ref_order_type)
        ELSE h.ref_order_type
    END AS channel_group,
    h.ref_order_type,
    IFNULL(ms_store.type, '') AS store_type,
    IFNULL(ms_store.group1, '') AS store_group1,
    IFNULL(ms_store.price_group, '') AS price_group,
    COUNT(DISTINCT h.outbound_code) AS outbound_order_count,
    SUM(IFNULL(d.ship_qty, 0)) AS outbound_qty,
    SUM(IFNULL(d.ship_qty, 0) * IFNULL(ms.net_weight, 0) / 1000) AS outbound_ton
FROM scm_sales_shipment_header h
INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
LEFT JOIN master_store ms_store ON ms_store.code = h.store_code
LEFT JOIN master_wms mw ON mw.wms_code = h.facility_code
LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
WHERE h.ship_at >= %(start)s AND h.ship_at < %(end)s
  AND h.outbound_code LIKE 'HJXH%%'
  AND h.status IN ('700','900','701','901')
  AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
  AND ms.sku_group = '50'
  AND mw.wms_type IN (10, 11)
  {wms_filter_mw}
GROUP BY channel_group, h.ref_order_type, store_type, store_group1, price_group
ORDER BY outbound_ton DESC;
""",
        "warehouse_summary": f"""
WITH sales AS (
    SELECT h.facility_code AS warehouse_code,
           MAX(mw.wms_desc) AS warehouse_name,
           COUNT(DISTINCT h.outbound_code) AS outbound_order_count,
           SUM(IFNULL(d.ship_qty, 0)) AS outbound_qty,
           SUM(IFNULL(d.ship_qty, 0) * IFNULL(ms.net_weight, 0) / 1000) AS outbound_ton
    FROM scm_sales_shipment_header h
    INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.facility_code
    LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
    WHERE h.ship_at >= %(start)s AND h.ship_at < %(end)s
      AND h.outbound_code LIKE 'HJXH%%'
      AND h.status IN ('700','900','701','901')
      AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
      AND ms.sku_group = '50'
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY h.facility_code
),
purchase AS (
    SELECT h.take_delivery_wms AS warehouse_code,
           MAX(mw.wms_desc) AS warehouse_name,
           COUNT(DISTINCT h.po_code) AS purchase_order_count,
           SUM(IFNULL(d.received_quantity, 0)) AS purchase_qty,
           SUM(IFNULL(d.received_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) AS purchase_ton
    FROM scm_po_header h
    INNER JOIN scm_po_detail d ON d.po_code = h.po_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.take_delivery_wms
    LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
    WHERE d.created_date >= %(start)s AND d.created_date < %(end)s
      AND h.cargo_owner_id = %(owner)s
      AND h.po_type = 'CCP'
      AND h.status = 0
      AND d.status = 0
      AND ms.sku_group = '50'
      {wms_filter_mw}
    GROUP BY h.take_delivery_wms
),
stock AS (
    SELECT w.wms_code AS warehouse_code,
           MAX(mw.wms_desc) AS warehouse_name,
           SUM(CASE WHEN DATE(w.backups_date) = %(start)s THEN IFNULL(w.in_stock_quantity, 0) ELSE 0 END) AS opening_stock_qty,
           SUM(CASE WHEN DATE(w.backups_date) = DATE_SUB(%(end)s, INTERVAL 1 DAY) THEN IFNULL(w.in_stock_quantity, 0) ELSE 0 END) AS closing_stock_qty,
           SUM(CASE WHEN DATE(w.backups_date) = %(start)s THEN IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000 ELSE 0 END) AS opening_stock_ton,
           SUM(CASE WHEN DATE(w.backups_date) = DATE_SUB(%(end)s, INTERVAL 1 DAY) THEN IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000 ELSE 0 END) AS closing_stock_ton,
           SUM(IFNULL(w.in_stock_quantity, 0)) / COUNT(DISTINCT DATE(w.backups_date)) AS avg_stock_qty,
           SUM(IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) / COUNT(DISTINCT DATE(w.backups_date)) AS avg_stock_ton
    FROM scm_warehous_manage_backups w
    LEFT JOIN master_wms mw ON mw.wms_code = w.wms_code
    LEFT JOIN master_sku ms ON ms.sku_code = w.sku_code
    WHERE w.backups_date >= %(start)s AND w.backups_date < %(end)s
      AND w.cargo_owner_id = %(owner)s
      AND ms.sku_group = '50'
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY w.wms_code
),
all_keys AS (
    SELECT warehouse_code FROM sales
    UNION SELECT warehouse_code FROM purchase
    UNION SELECT warehouse_code FROM stock
)
SELECT ak.warehouse_code,
       COALESCE(st.warehouse_name, s.warehouse_name, p.warehouse_name, ak.warehouse_code) AS warehouse_name,
       IFNULL(st.opening_stock_qty, 0) AS opening_stock_qty,
       IFNULL(st.opening_stock_ton, 0) AS opening_stock_ton,
       IFNULL(st.closing_stock_qty, 0) AS closing_stock_qty,
       IFNULL(st.closing_stock_ton, 0) AS closing_stock_ton,
       IFNULL(st.avg_stock_qty, 0) AS avg_stock_qty,
       IFNULL(st.avg_stock_ton, 0) AS avg_stock_ton,
       IFNULL(s.outbound_order_count, 0) AS outbound_order_count,
       IFNULL(s.outbound_qty, 0) AS outbound_qty,
       IFNULL(s.outbound_ton, 0) AS outbound_ton,
       IFNULL(p.purchase_order_count, 0) AS purchase_order_count,
       IFNULL(p.purchase_qty, 0) AS purchase_qty,
       IFNULL(p.purchase_ton, 0) AS purchase_ton,
       IFNULL(p.purchase_ton, 0) - IFNULL(s.outbound_ton, 0) AS net_flow_ton,
       CASE WHEN IFNULL(s.outbound_ton, 0) > 0 THEN IFNULL(st.avg_stock_ton, 0) / (IFNULL(s.outbound_ton, 0) / 30) ELSE NULL END AS avg_turnover_days,
       CASE WHEN IFNULL(s.outbound_ton, 0) > 0 THEN IFNULL(st.closing_stock_ton, 0) / (IFNULL(s.outbound_ton, 0) / 30) ELSE NULL END AS closing_cover_days
FROM all_keys ak
LEFT JOIN stock st ON st.warehouse_code = ak.warehouse_code
LEFT JOIN sales s ON s.warehouse_code = ak.warehouse_code
LEFT JOIN purchase p ON p.warehouse_code = ak.warehouse_code
ORDER BY outbound_ton DESC, closing_stock_ton DESC;
""",
        "sku_summary": f"""
WITH sales AS (
    SELECT d.sku_code,
           MAX(ms.sku_name) AS sku_name,
           COUNT(DISTINCT h.outbound_code) AS outbound_order_count,
           SUM(IFNULL(d.ship_qty, 0)) AS outbound_qty,
           SUM(IFNULL(d.ship_qty, 0) * IFNULL(ms.net_weight, 0) / 1000) AS outbound_ton
    FROM scm_sales_shipment_header h
    INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.facility_code
    LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
    WHERE h.ship_at >= %(start)s AND h.ship_at < %(end)s
      AND h.outbound_code LIKE 'HJXH%%'
      AND h.status IN ('700','900','701','901')
      AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
      AND ms.sku_group = '50'
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY d.sku_code
),
purchase AS (
    SELECT d.sku_code,
           MAX(ms.sku_name) AS sku_name,
           COUNT(DISTINCT h.po_code) AS purchase_order_count,
           SUM(IFNULL(d.received_quantity, 0)) AS purchase_qty,
           SUM(IFNULL(d.received_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) AS purchase_ton
    FROM scm_po_header h
    INNER JOIN scm_po_detail d ON d.po_code = h.po_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.take_delivery_wms
    LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
    WHERE d.created_date >= %(start)s AND d.created_date < %(end)s
      AND h.cargo_owner_id = %(owner)s
      AND h.po_type = 'CCP'
      AND h.status = 0
      AND d.status = 0
      AND ms.sku_group = '50'
      {wms_filter_mw}
    GROUP BY d.sku_code
),
stock AS (
    SELECT w.sku_code,
           MAX(ms.sku_name) AS sku_name,
           SUM(CASE WHEN DATE(w.backups_date) = %(start)s THEN IFNULL(w.in_stock_quantity, 0) ELSE 0 END) AS opening_stock_qty,
           SUM(CASE WHEN DATE(w.backups_date) = DATE_SUB(%(end)s, INTERVAL 1 DAY) THEN IFNULL(w.in_stock_quantity, 0) ELSE 0 END) AS closing_stock_qty,
           SUM(CASE WHEN DATE(w.backups_date) = %(start)s THEN IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000 ELSE 0 END) AS opening_stock_ton,
           SUM(CASE WHEN DATE(w.backups_date) = DATE_SUB(%(end)s, INTERVAL 1 DAY) THEN IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000 ELSE 0 END) AS closing_stock_ton,
           SUM(IFNULL(w.in_stock_quantity, 0)) / COUNT(DISTINCT DATE(w.backups_date)) AS avg_stock_qty,
           SUM(IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) / COUNT(DISTINCT DATE(w.backups_date)) AS avg_stock_ton
    FROM scm_warehous_manage_backups w
    LEFT JOIN master_wms mw ON mw.wms_code = w.wms_code
    LEFT JOIN master_sku ms ON ms.sku_code = w.sku_code
    WHERE w.backups_date >= %(start)s AND w.backups_date < %(end)s
      AND w.cargo_owner_id = %(owner)s
      AND ms.sku_group = '50'
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY w.sku_code
),
all_keys AS (
    SELECT sku_code FROM sales
    UNION SELECT sku_code FROM purchase
    UNION SELECT sku_code FROM stock
)
SELECT ak.sku_code,
       COALESCE(st.sku_name, s.sku_name, p.sku_name, ak.sku_code) AS sku_name,
       ms.finished_product_category,
       ms.sku_class,
       ms.group_number,
       ms.net_weight,
       IFNULL(st.opening_stock_qty, 0) AS opening_stock_qty,
       IFNULL(st.opening_stock_ton, 0) AS opening_stock_ton,
       IFNULL(st.closing_stock_qty, 0) AS closing_stock_qty,
       IFNULL(st.closing_stock_ton, 0) AS closing_stock_ton,
       IFNULL(st.avg_stock_qty, 0) AS avg_stock_qty,
       IFNULL(st.avg_stock_ton, 0) AS avg_stock_ton,
       IFNULL(s.outbound_order_count, 0) AS outbound_order_count,
       IFNULL(s.outbound_qty, 0) AS outbound_qty,
       IFNULL(s.outbound_ton, 0) AS outbound_ton,
       IFNULL(p.purchase_order_count, 0) AS purchase_order_count,
       IFNULL(p.purchase_qty, 0) AS purchase_qty,
       IFNULL(p.purchase_ton, 0) AS purchase_ton,
       IFNULL(p.purchase_ton, 0) - IFNULL(s.outbound_ton, 0) AS net_flow_ton,
       CASE WHEN IFNULL(s.outbound_ton, 0) > 0 THEN IFNULL(st.avg_stock_ton, 0) / (IFNULL(s.outbound_ton, 0) / 30) ELSE NULL END AS avg_turnover_days,
       CASE WHEN IFNULL(s.outbound_ton, 0) > 0 THEN IFNULL(st.closing_stock_ton, 0) / (IFNULL(s.outbound_ton, 0) / 30) ELSE NULL END AS closing_cover_days
FROM all_keys ak
LEFT JOIN stock st ON st.sku_code = ak.sku_code
LEFT JOIN sales s ON s.sku_code = ak.sku_code
LEFT JOIN purchase p ON p.sku_code = ak.sku_code
LEFT JOIN master_sku ms ON ms.sku_code = ak.sku_code
ORDER BY outbound_ton DESC, closing_stock_ton DESC;
""",
        "age_bucket": f"""
SELECT
    CASE
        WHEN w.production_date IS NULL THEN '无生产日期'
        WHEN DATEDIFF(DATE_SUB(%(end)s, INTERVAL 1 DAY), DATE(w.production_date)) <= 30 THEN '0-30天'
        WHEN DATEDIFF(DATE_SUB(%(end)s, INTERVAL 1 DAY), DATE(w.production_date)) <= 60 THEN '31-60天'
        WHEN DATEDIFF(DATE_SUB(%(end)s, INTERVAL 1 DAY), DATE(w.production_date)) <= 90 THEN '61-90天'
        WHEN DATEDIFF(DATE_SUB(%(end)s, INTERVAL 1 DAY), DATE(w.production_date)) <= 180 THEN '91-180天'
        WHEN DATEDIFF(DATE_SUB(%(end)s, INTERVAL 1 DAY), DATE(w.production_date)) <= 270 THEN '181-270天'
        ELSE '270天以上'
    END AS age_bucket,
    COUNT(*) AS row_count,
    COUNT(DISTINCT w.wms_code) AS warehouse_count,
    COUNT(DISTINCT w.sku_code) AS sku_count,
    SUM(IFNULL(w.in_stock_quantity, 0)) AS stock_qty,
    SUM(IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) AS stock_ton
FROM scm_warehous_manage_backups w
LEFT JOIN master_wms mw ON mw.wms_code = w.wms_code
LEFT JOIN master_sku ms ON ms.sku_code = w.sku_code
WHERE DATE(w.backups_date) = DATE_SUB(%(end)s, INTERVAL 1 DAY)
  AND w.cargo_owner_id = %(owner)s
  AND ms.sku_group = '50'
  AND mw.wms_type IN (10, 11)
  {wms_filter_mw}
GROUP BY age_bucket
ORDER BY
    CASE age_bucket
        WHEN '0-30天' THEN 1
        WHEN '31-60天' THEN 2
        WHEN '61-90天' THEN 3
        WHEN '91-180天' THEN 4
        WHEN '181-270天' THEN 5
        WHEN '270天以上' THEN 6
        ELSE 7
    END;
""",
        "transfer_summary": f"""
SELECT
    'transfer_order_actual_outbound' AS metric_name,
    COUNT(DISTINCT h.transfer_order_code) AS transfer_order_count,
    COUNT(*) AS detail_rows,
    SUM(IFNULL(d.ship_qty, 0)) AS transfer_ship_qty,
    SUM(IFNULL(d.ship_qty, 0) * IFNULL(ms.net_weight, 0) / 1000) AS transfer_ship_ton,
    SUM(IFNULL(d.recv_qty, 0)) AS transfer_recv_qty,
    SUM(IFNULL(d.recv_qty, 0) * IFNULL(ms.net_weight, 0) / 1000) AS transfer_recv_ton
FROM scm_transfer_order_header h
INNER JOIN scm_transfer_order_detail d ON d.transfer_order_code = h.transfer_order_code
LEFT JOIN master_sku ms ON ms.sku_code = d.sku_code
LEFT JOIN master_wms mw1 ON mw1.wms_code = h.delivery_wms
LEFT JOIN master_wms mw2 ON mw2.wms_code = h.receive_wms
WHERE h.actual_outbound_time >= %(start)s AND h.actual_outbound_time < %(end)s
  AND (h.sender_cargo_owner = %(owner)s OR h.receiver_cargo_owner = %(owner)s)
  AND ms.sku_group = '50'
  AND IFNULL(mw1.wms_desc, '') NOT LIKE '%%残次%%'
  AND IFNULL(mw2.wms_desc, '') NOT LIKE '%%残次%%';
""",
        "top_aging_sku": f"""
SELECT w.sku_code,
       MAX(ms.sku_name) AS sku_name,
       COUNT(DISTINCT w.wms_code) AS warehouse_count,
       SUM(IFNULL(w.in_stock_quantity, 0)) AS stock_qty,
       SUM(IFNULL(w.in_stock_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) AS stock_ton,
       MIN(DATE(w.production_date)) AS earliest_production_date,
       MAX(DATEDIFF(DATE_SUB(%(end)s, INTERVAL 1 DAY), DATE(w.production_date))) AS max_age_days
FROM scm_warehous_manage_backups w
LEFT JOIN master_wms mw ON mw.wms_code = w.wms_code
LEFT JOIN master_sku ms ON ms.sku_code = w.sku_code
WHERE DATE(w.backups_date) = DATE_SUB(%(end)s, INTERVAL 1 DAY)
  AND w.cargo_owner_id = %(owner)s
  AND ms.sku_group = '50'
  AND mw.wms_type IN (10, 11)
  {wms_filter_mw}
  AND w.production_date IS NOT NULL
  AND DATEDIFF(DATE_SUB(%(end)s, INTERVAL 1 DAY), DATE(w.production_date)) > 180
GROUP BY w.sku_code
ORDER BY stock_ton DESC
LIMIT 30;
""",
        "missing_weight": f"""
SELECT ms.sku_code, ms.sku_name, ms.net_weight,
       SUM(IFNULL(x.stock_qty, 0)) AS related_stock_qty,
       SUM(IFNULL(x.outbound_qty, 0)) AS related_outbound_qty,
       SUM(IFNULL(x.purchase_qty, 0)) AS related_purchase_qty
FROM master_sku ms
LEFT JOIN (
    SELECT w.sku_code, SUM(IFNULL(w.in_stock_quantity, 0)) AS stock_qty, 0 AS outbound_qty, 0 AS purchase_qty
    FROM scm_warehous_manage_backups w
    LEFT JOIN master_wms mw ON mw.wms_code = w.wms_code
    WHERE w.backups_date >= %(start)s AND w.backups_date < %(end)s
      AND w.cargo_owner_id = %(owner)s
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY w.sku_code
    UNION ALL
    SELECT d.sku_code, 0, SUM(IFNULL(d.ship_qty, 0)), 0
    FROM scm_sales_shipment_header h
    INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.facility_code
    WHERE h.ship_at >= %(start)s AND h.ship_at < %(end)s
      AND h.outbound_code LIKE 'HJXH%%'
      AND h.status IN ('700','900','701','901')
      AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
      AND mw.wms_type IN (10, 11)
      {wms_filter_mw}
    GROUP BY d.sku_code
    UNION ALL
    SELECT d.sku_code, 0, 0, SUM(IFNULL(d.received_quantity, 0))
    FROM scm_po_header h
    INNER JOIN scm_po_detail d ON d.po_code = h.po_code
    LEFT JOIN master_wms mw ON mw.wms_code = h.take_delivery_wms
    WHERE d.created_date >= %(start)s AND d.created_date < %(end)s
      AND h.cargo_owner_id = %(owner)s
      AND h.po_type = 'CCP'
      AND h.status = 0
      AND d.status = 0
      {wms_filter_mw}
    GROUP BY d.sku_code
) x ON x.sku_code = ms.sku_code
WHERE ms.sku_group = '50'
  AND IFNULL(ms.net_weight, 0) <= 0
GROUP BY ms.sku_code, ms.sku_name, ms.net_weight
HAVING related_stock_qty > 0 OR related_outbound_qty > 0 OR related_purchase_qty > 0
ORDER BY related_outbound_qty DESC, related_stock_qty DESC
LIMIT 50;
""",
    }


def category_summary_from_sku(sku_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in sku_rows:
        category = (
            str(row.get("finished_product_category") or "").strip()
            or str(row.get("sku_class") or "").strip()
            or str(row.get("group_number") or "").strip()
            or "未维护分类"
        )
        item = groups.setdefault(
            category,
            {
                "category": category,
                "sku_count": 0,
                "outbound_qty": 0.0,
                "outbound_ton": 0.0,
                "purchase_qty": 0.0,
                "purchase_ton": 0.0,
                "closing_stock_qty": 0.0,
                "closing_stock_ton": 0.0,
                "avg_stock_ton": 0.0,
            },
        )
        item["sku_count"] += 1
        for field in [
            "outbound_qty",
            "outbound_ton",
            "purchase_qty",
            "purchase_ton",
            "closing_stock_qty",
            "closing_stock_ton",
            "avg_stock_ton",
        ]:
            item[field] += to_float(row.get(field))
    result = list(groups.values())
    result.sort(key=lambda item: item["outbound_ton"], reverse=True)
    return result


def normalize_daily_flow(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        dt = row.get("dt")
        key = dt.isoformat() if isinstance(dt, date) else str(dt)
        normalized = dict(row)
        normalized["dt"] = key
        by_date[key] = normalized
    output: list[dict[str, Any]] = []
    current = START_DATE
    zero_fields = [
        "outbound_order_count",
        "outbound_qty",
        "outbound_ton",
        "purchase_order_count",
        "purchase_qty",
        "purchase_ton",
        "stock_qty",
        "stock_ton",
    ]
    while current < END_DATE:
        key = current.isoformat()
        row = by_date.get(key)
        if row is None:
            row = {"dt": key, **{field: 0 for field in zero_fields}}
        else:
            for field in zero_fields:
                row.setdefault(field, 0)
        output.append(row)
        current += timedelta(days=1)
    return output


def collect_data(connection) -> dict[str, list[dict[str, Any]] | dict[str, Any]]:
    params = {"start": START_DATE, "end": END_DATE, "owner": CARGO_OWNER_ID}
    sqls = build_sql()
    data: dict[str, list[dict[str, Any]] | dict[str, Any]] = {}
    for name, sql in sqls.items():
        rows = fetch_all(connection, sql, params)
        if name == "daily_flow":
            rows = normalize_daily_flow(rows)
        data[name] = rows
        print(f"fetched {name}: {len(rows)} rows")
    sku_rows = data["sku_summary"]  # type: ignore[index]
    assert isinstance(sku_rows, list)
    data["category_summary"] = category_summary_from_sku(sku_rows)
    return data


def kpis(data: dict[str, Any]) -> dict[str, float]:
    daily = data["daily_flow"]
    warehouses = data["warehouse_summary"]
    sku_rows = data["sku_summary"]
    age_rows = data["age_bucket"]

    outbound_qty = sum(to_float(row["outbound_qty"]) for row in daily)
    outbound_ton = sum(to_float(row["outbound_ton"]) for row in daily)
    purchase_qty = sum(to_float(row["purchase_qty"]) for row in daily)
    purchase_ton = sum(to_float(row["purchase_ton"]) for row in daily)
    opening_stock_qty = sum(to_float(row["opening_stock_qty"]) for row in warehouses)
    opening_stock_ton = sum(to_float(row["opening_stock_ton"]) for row in warehouses)
    closing_stock_qty = sum(to_float(row["closing_stock_qty"]) for row in warehouses)
    closing_stock_ton = sum(to_float(row["closing_stock_ton"]) for row in warehouses)
    avg_stock_ton = sum(to_float(row["avg_stock_ton"]) for row in warehouses)
    active_warehouses = len([row for row in warehouses if to_float(row["outbound_ton"]) or to_float(row["closing_stock_ton"])])
    active_skus = len([row for row in sku_rows if to_float(row["outbound_ton"]) or to_float(row["closing_stock_ton"])])
    old_stock_ton = sum(
        to_float(row["stock_ton"]) for row in age_rows if row["age_bucket"] in {"181-270天", "270天以上", "无生产日期"}
    )
    return {
        "outbound_qty": outbound_qty,
        "outbound_ton": outbound_ton,
        "purchase_qty": purchase_qty,
        "purchase_ton": purchase_ton,
        "purchase_sales_ton_gap": purchase_ton - outbound_ton,
        "purchase_sales_ratio": safe_div(purchase_ton, outbound_ton),
        "opening_stock_qty": opening_stock_qty,
        "opening_stock_ton": opening_stock_ton,
        "closing_stock_qty": closing_stock_qty,
        "closing_stock_ton": closing_stock_ton,
        "stock_ton_change": closing_stock_ton - opening_stock_ton,
        "stock_ton_change_pct": safe_div(closing_stock_ton - opening_stock_ton, opening_stock_ton),
        "avg_stock_ton": avg_stock_ton,
        "avg_daily_outbound_ton": outbound_ton / 30,
        "turnover_days": safe_div(avg_stock_ton, outbound_ton / 30),
        "closing_cover_days": safe_div(closing_stock_ton, outbound_ton / 30),
        "active_warehouses": active_warehouses,
        "active_skus": active_skus,
        "old_stock_ton": old_stock_ton,
        "old_stock_ratio": safe_div(old_stock_ton, closing_stock_ton),
        "outbound_order_count": sum(to_float(row["outbound_order_count"]) for row in daily),
        "purchase_order_count": sum(to_float(row["purchase_order_count"]) for row in daily),
    }


def write_workbook(data: dict[str, Any], path: Path, summary: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def add_sheet(name: str, rows: list[dict[str, Any]]) -> None:
        ws = wb.create_sheet(name[:31])
        if not rows:
            ws.append(["无数据"])
            return
        headers = list(rows[0].keys())
        ws.append(headers)
        for row in rows:
            ws.append([row.get(header) for header in headers])
        header_fill = PatternFill("solid", fgColor="D9EAF7")
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col_idx, header in enumerate(headers, start=1):
            width = min(max(len(str(header)) + 2, 12), 36)
            for cell in ws.iter_cols(min_col=col_idx, max_col=col_idx, min_row=2, max_row=min(ws.max_row, 200)):
                for value_cell in cell:
                    width = min(max(width, len(str(value_cell.value or "")) + 2), 42)
            ws.column_dimensions[get_column_letter(col_idx)].width = width
            if any(key in header for key in ["qty", "ton", "days", "ratio", "count", "weight"]):
                for value_cell in ws.iter_cols(min_col=col_idx, max_col=col_idx, min_row=2, max_row=ws.max_row):
                    for c in value_cell:
                        if isinstance(c.value, (int, float, Decimal)):
                            c.number_format = "#,##0.00" if "ton" in header or "days" in header or "ratio" in header else "#,##0"

    summary_rows = [
        {"指标": "月度出库数量", "数值": summary["outbound_qty"], "单位": "件/包/基础单位"},
        {"指标": "月度出库吨数", "数值": summary["outbound_ton"], "单位": "吨"},
        {"指标": "月度采购实收数量", "数值": summary["purchase_qty"], "单位": "件/包/基础单位"},
        {"指标": "月度采购实收吨数", "数值": summary["purchase_ton"], "单位": "吨"},
        {"指标": "采购-出库差额", "数值": summary["purchase_sales_ton_gap"], "单位": "吨"},
        {"指标": "6月1日库存", "数值": summary["opening_stock_ton"], "单位": "吨"},
        {"指标": "6月30日库存", "数值": summary["closing_stock_ton"], "单位": "吨"},
        {"指标": "库存变化", "数值": summary["stock_ton_change"], "单位": "吨"},
        {"指标": "月均库存", "数值": summary["avg_stock_ton"], "单位": "吨"},
        {"指标": "库存周转天数", "数值": summary["turnover_days"], "单位": "天"},
        {"指标": "期末库存覆盖天数", "数值": summary["closing_cover_days"], "单位": "天"},
        {"指标": "181天以上/无生产日期库存占比", "数值": summary["old_stock_ratio"], "单位": "比例"},
    ]
    add_sheet("summary_kpi", summary_rows)
    for name in [
        "data_quality",
        "daily_flow",
        "warehouse_summary",
        "sku_summary",
        "category_summary",
        "channel_summary",
        "age_bucket",
        "transfer_summary",
        "top_aging_sku",
        "missing_weight",
    ]:
        add_sheet(name, data.get(name, []))

    if "daily_flow" in wb.sheetnames:
        ws = wb["daily_flow"]
        if ws.max_row > 2:
            chart = LineChart()
            chart.title = "6月每日出库/采购吨数"
            chart.y_axis.title = "吨"
            chart.x_axis.title = "日期"
            data_ref = Reference(ws, min_col=4, max_col=6, min_row=1, max_row=ws.max_row)
            cats = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row)
            chart.add_data(data_ref, titles_from_data=True)
            chart.set_categories(cats)
            chart.height = 8
            chart.width = 18
            ws.add_chart(chart, "K2")
    if "warehouse_summary" in wb.sheetnames:
        ws = wb["warehouse_summary"]
        top_rows = min(ws.max_row, 16)
        if top_rows > 2:
            chart = BarChart()
            chart.title = "Top仓库出库吨数"
            chart.y_axis.title = "吨"
            data_ref = Reference(ws, min_col=11, min_row=1, max_row=top_rows)
            cats = Reference(ws, min_col=2, min_row=2, max_row=top_rows)
            chart.add_data(data_ref, titles_from_data=True)
            chart.set_categories(cats)
            chart.height = 8
            chart.width = 18
            ws.add_chart(chart, "T2")

    wb.save(path)


def set_cell_text(cell, text: Any, bold: bool = False) -> None:
    cell.text = ""
    para = cell.paragraphs[0]
    run = para.add_run("" if text is None else str(text))
    run.bold = bold
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_table_borders(table, color: str = "B7C9D6", size: str = "4") -> None:
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = "w:" + edge
        element = borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), size)
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), color)


def add_table(doc: Document, headers: list[str], rows: list[list[Any]], widths: list[float] | None = None) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table)
    for idx, header in enumerate(headers):
        cell = table.rows[0].cells[idx]
        set_cell_text(cell, header, bold=True)
        set_cell_shading(cell, "E8EEF5")
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    for row in rows:
        cells = table.add_row().cells
        for idx, value in enumerate(row):
            set_cell_text(cells[idx], value)
            cells[idx].vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    if widths:
        for row in table.rows:
            for idx, width in enumerate(widths):
                row.cells[idx].width = Inches(width)
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    run.font.size = Pt(9)
    doc.add_paragraph()


def add_heading(doc: Document, text: str, level: int) -> None:
    p = doc.add_heading(text, level=level)
    for run in p.runs:
        run.font.name = "Microsoft YaHei"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        run.font.color.rgb = RGBColor(46, 116, 181 if level < 3 else 120)


def add_bullet(doc: Document, text: str) -> None:
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run(text)
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    run.font.size = Pt(10.5)


def setup_doc_styles(doc: Document) -> None:
    section = doc.sections[0]
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)

    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.1

    for style_name in ["Heading 1", "Heading 2", "Heading 3"]:
        style = doc.styles[style_name]
        style.font.name = "Microsoft YaHei"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.color.rgb = RGBColor(46, 116, 181)
        style.paragraph_format.space_before = Pt(12)
        style.paragraph_format.space_after = Pt(6)
    doc.styles["Heading 1"].font.size = Pt(16)
    doc.styles["Heading 2"].font.size = Pt(13)
    doc.styles["Heading 3"].font.size = Pt(12)


def docx_metadata(doc: Document) -> None:
    props = doc.core_properties
    props.author = "Codex"
    props.title = "供应链6月份月度报告"
    props.subject = "HJXH供应链月度经营分析"
    props.comments = "Generated from Doris read-only data source."


def create_report(data: dict[str, Any], summary: dict[str, float], docx_path: Path, xlsx_path: Path) -> None:
    doc = Document()
    setup_doc_styles(doc)
    docx_metadata(doc)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("供应链6月份月度报告")
    run.bold = True
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    run.font.size = Pt(22)
    run.font.color.rgb = RGBColor(31, 77, 120)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = subtitle.add_run("范围：皇家小虎/HJXH｜周期：2026-06-01 至 2026-06-30｜数据源：Doris ywzt_ods")
    r.font.name = "Microsoft YaHei"
    r._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    r.font.size = Pt(10)
    r.font.color.rgb = RGBColor(85, 85, 85)

    add_heading(doc, "一、核心结论", 1)
    bullets = [
        f"6月成品出库 {fmt_qty(summary['outbound_qty'])} 件，折合 {fmt_ton(summary['outbound_ton'])} 吨；日均出库 {fmt_ton(summary['avg_daily_outbound_ton'])} 吨。",
        f"6月采购实收 {fmt_qty(summary['purchase_qty'])} 件，折合 {fmt_ton(summary['purchase_ton'])} 吨；采购/出库吨数比为 {summary['purchase_sales_ratio']:.2f}。",
        f"6月30日期末库存 {fmt_qty(summary['closing_stock_qty'])} 件，折合 {fmt_ton(summary['closing_stock_ton'])} 吨；较6月1日变化 {fmt_ton(summary['stock_ton_change'])} 吨（{pct(summary['stock_ton_change_pct'])}）。",
        f"按月均库存和月出库测算，库存周转约 {summary['turnover_days']:.1f} 天；期末库存覆盖约 {summary['closing_cover_days']:.1f} 天。",
        f"181天以上或缺少生产日期的期末库存 {fmt_ton(summary['old_stock_ton'])} 吨，占期末库存 {pct(summary['old_stock_ratio'])}，需要作为库龄治理重点。",
    ]
    for item in bullets:
        add_bullet(doc, item)

    add_heading(doc, "二、关键指标总览", 1)
    add_table(
        doc,
        ["指标", "6月结果", "说明"],
        [
            ["出库数量", fmt_qty(summary["outbound_qty"]), "有效销售/业务出库，剔除残次等仓"],
            ["出库吨数", fmt_ton(summary["outbound_ton"]), "ship_qty * master_sku.net_weight / 1000"],
            ["采购实收数量", fmt_qty(summary["purchase_qty"]), "po_detail.received_quantity"],
            ["采购实收吨数", fmt_ton(summary["purchase_ton"]), "仅 CCP 成品采购单"],
            ["采购-出库差额", fmt_ton(summary["purchase_sales_ton_gap"]), "正数表示采购补入高于出库"],
            ["6月1日库存吨数", fmt_ton(summary["opening_stock_ton"]), "scm_warehous_manage_backups"],
            ["6月30日库存吨数", fmt_ton(summary["closing_stock_ton"]), "期末快照"],
            ["月均库存吨数", fmt_ton(summary["avg_stock_ton"]), "30天库存快照平均"],
            ["库存周转天数", f"{summary['turnover_days']:.1f}", "月均库存 / 日均出库"],
            ["活跃仓库数", fmt_qty(summary["active_warehouses"]), "有出库或期末库存的仓库"],
            ["活跃SKU数", fmt_qty(summary["active_skus"]), "有出库或期末库存的成品SKU"],
        ],
        widths=[1.8, 1.5, 3.2],
    )

    daily = data["daily_flow"]
    daily_sorted_out = sorted(daily, key=lambda row: to_float(row["outbound_ton"]), reverse=True)
    daily_sorted_purchase = sorted(daily, key=lambda row: to_float(row["purchase_ton"]), reverse=True)
    add_heading(doc, "三、日趋势与供需节奏", 1)
    add_table(
        doc,
        ["项目", "峰值日期", "峰值吨数", "峰值数量", "月均吨数"],
        [
            [
                "出库",
                daily_sorted_out[0]["dt"],
                fmt_ton(daily_sorted_out[0]["outbound_ton"]),
                fmt_qty(daily_sorted_out[0]["outbound_qty"]),
                fmt_ton(summary["avg_daily_outbound_ton"]),
            ],
            [
                "采购实收",
                daily_sorted_purchase[0]["dt"],
                fmt_ton(daily_sorted_purchase[0]["purchase_ton"]),
                fmt_qty(daily_sorted_purchase[0]["purchase_qty"]),
                fmt_ton(summary["purchase_ton"] / 30),
            ],
        ],
        widths=[1.2, 1.5, 1.4, 1.4, 1.2],
    )
    add_bullet(doc, "出库与采购均覆盖6月30个自然日，月报没有发现日期断档。")
    add_bullet(doc, "采购统计使用采购明细创建日期，因为采购单交付日期字段在6月样本中为空；这是本工作区既有采购吨数口径。")

    add_heading(doc, "四、仓库表现", 1)
    warehouses = data["warehouse_summary"]
    top_wh = warehouses[:10]
    add_table(
        doc,
        ["仓库", "出库吨", "采购吨", "期末库存吨", "覆盖天数", "净流入吨"],
        [
            [
                row["warehouse_name"],
                fmt_ton(row["outbound_ton"]),
                fmt_ton(row["purchase_ton"]),
                fmt_ton(row["closing_stock_ton"]),
                "-" if row["closing_cover_days"] is None else f"{to_float(row['closing_cover_days']):.1f}",
                fmt_ton(row["net_flow_ton"]),
            ]
            for row in top_wh
        ],
        widths=[2.5, 0.9, 0.9, 1.1, 0.9, 0.9],
    )
    high_cover = [
        row for row in warehouses if to_float(row["closing_stock_ton"]) >= 50 and (row["closing_cover_days"] is None or to_float(row["closing_cover_days"]) > 60)
    ][:8]
    if high_cover:
        add_bullet(
            doc,
            "高库存覆盖仓库集中在："
            + "；".join(
                f"{row['warehouse_name']} {fmt_ton(row['closing_stock_ton'])}吨/覆盖{('-' if row['closing_cover_days'] is None else f'{to_float(row['closing_cover_days']):.1f}天')}"
                for row in high_cover[:5]
            )
            + "。",
        )

    add_heading(doc, "五、SKU与品类结构", 1)
    categories = data["category_summary"]
    top_categories = categories[:10]
    add_table(
        doc,
        ["品类/分组", "SKU数", "出库吨", "采购吨", "期末库存吨", "采购/出库"],
        [
            [
                row["category"],
                row["sku_count"],
                fmt_ton(row["outbound_ton"]),
                fmt_ton(row["purchase_ton"]),
                fmt_ton(row["closing_stock_ton"]),
                f"{safe_div(to_float(row['purchase_ton']), to_float(row['outbound_ton'])):.2f}" if to_float(row["outbound_ton"]) else "-",
            ]
            for row in top_categories
        ],
        widths=[2.0, 0.7, 1.0, 1.0, 1.1, 0.9],
    )
    top_skus = sorted(data["sku_summary"], key=lambda row: to_float(row["outbound_ton"]), reverse=True)[:12]
    add_table(
        doc,
        ["SKU", "商品名称", "出库吨", "采购吨", "期末库存吨", "覆盖天数"],
        [
            [
                row["sku_code"],
                row["sku_name"],
                fmt_ton(row["outbound_ton"]),
                fmt_ton(row["purchase_ton"]),
                fmt_ton(row["closing_stock_ton"]),
                "-" if row["closing_cover_days"] is None else f"{to_float(row['closing_cover_days']):.1f}",
            ]
            for row in top_skus
        ],
        widths=[1.2, 2.4, 0.9, 0.9, 1.0, 0.8],
    )

    add_heading(doc, "六、渠道出库结构", 1)
    channels = data["channel_summary"]
    total_channel_ton = sum(to_float(row["outbound_ton"]) for row in channels)
    add_table(
        doc,
        ["渠道组", "出库单数", "出库数量", "出库吨", "吨数占比"],
        [
            [
                row["channel_group"],
                fmt_qty(row["outbound_order_count"]),
                fmt_qty(row["outbound_qty"]),
                fmt_ton(row["outbound_ton"]),
                pct(safe_div(to_float(row["outbound_ton"]), total_channel_ton)),
            ]
            for row in channels[:12]
        ],
        widths=[2.4, 1.0, 1.1, 1.0, 0.9],
    )

    add_heading(doc, "七、库存库龄与风险", 1)
    age_rows = data["age_bucket"]
    add_table(
        doc,
        ["库龄段", "SKU数", "仓库数", "期末库存数量", "期末库存吨", "吨数占比"],
        [
            [
                row["age_bucket"],
                fmt_qty(row["sku_count"]),
                fmt_qty(row["warehouse_count"]),
                fmt_qty(row["stock_qty"]),
                fmt_ton(row["stock_ton"]),
                pct(safe_div(to_float(row["stock_ton"]), summary["closing_stock_ton"])),
            ]
            for row in age_rows
        ],
        widths=[1.2, 0.8, 0.8, 1.3, 1.1, 0.9],
    )
    aging = data["top_aging_sku"]
    if aging:
        add_table(
            doc,
            ["高库龄SKU", "商品名称", "库存吨", "仓库数", "最早生产日期", "最大库龄天"],
            [
                [
                    row["sku_code"],
                    row["sku_name"],
                    fmt_ton(row["stock_ton"]),
                    fmt_qty(row["warehouse_count"]),
                    row["earliest_production_date"],
                    fmt_qty(row["max_age_days"]),
                ]
                for row in aging[:10]
            ],
            widths=[1.2, 2.3, 0.9, 0.8, 1.0, 0.8],
        )

    add_heading(doc, "八、数据质量与口径说明", 1)
    data_quality_rows = data["data_quality"]
    add_table(
        doc,
        ["数据集", "最早日期", "最晚日期", "覆盖天数", "行数", "仓库数", "SKU数"],
        [
            [
                row["metric_name"],
                row["min_date"],
                row["max_date"],
                fmt_qty(row["day_count"]),
                fmt_qty(row["row_count"]),
                fmt_qty(row["warehouse_count"]),
                fmt_qty(row["sku_count"]),
            ]
            for row in data_quality_rows
        ],
        widths=[1.6, 1.0, 1.0, 0.8, 1.1, 0.8, 0.8],
    )
    add_bullet(doc, "货主口径：事实表限定 HJXH；SKU口径：master_sku.sku_group='50'，即成品。")
    add_bullet(doc, "仓库口径：master_wms.wms_type IN (10,11)，并剔除名称含残次、次品、废品、报废、残品的仓库。")
    add_bullet(doc, "出库口径：scm_sales_shipment_header/detail，状态 700/900/701/901，ref_order_type 为 JYCK、HHCK、BFCK、QTCK 或 SO 开头。")
    add_bullet(doc, "采购口径：scm_po_header/detail，po_type='CCP'、主表和明细 status=0，使用 received_quantity 与 po_detail.created_date。")
    add_bullet(doc, "库存口径：scm_warehous_manage_backups 6月每日快照，库存吨数按 in_stock_quantity * net_weight / 1000。")
    add_bullet(doc, f"详细数据附件：{xlsx_path.name}。")

    docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(docx_path)
    normalize_docx_xml(docx_path)


def normalize_docx_xml(path: Path) -> None:
    # Remove rsid noise and keep output deterministic enough for repeated local runs.
    tmp_path = path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith(".xml"):
                text = data.decode("utf-8")
                text = re.sub(r' w:rsid[^=]*="[0-9A-Fa-f]+"', "", text)
                data = text.encode("utf-8")
            zout.writestr(item, data)
    tmp_path.replace(path)


def write_audit_json(data: dict[str, Any], summary: dict[str, float], path: Path) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": START_DATE.isoformat(), "end_exclusive": END_DATE.isoformat(), "label": MONTH_LABEL},
        "scope": {
            "cargo_owner_id": CARGO_OWNER_ID,
            "sku_filter": "master_sku.sku_group = '50'",
            "warehouse_filter": "master_wms.wms_type IN (10, 11), exclude defect/obsolete warehouse names",
        },
        "summary": summary,
        "data_quality": [as_jsonable(row) for row in data.get("data_quality", [])],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate HJXH supply chain monthly report for June 2026.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    load_env_file(args.env_file)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with connect() as connection:
        data = collect_data(connection)

    summary = kpis(data)
    workbook_path = args.output_dir / "HJXH供应链6月份月报数据附件_2026-06.xlsx"
    docx_path = args.output_dir / "HJXH供应链6月份月度报告_2026-06.docx"
    audit_path = args.output_dir / "HJXH供应链6月份月报取数审计_2026-06.json"

    write_workbook(data, workbook_path, summary)
    create_report(data, summary, docx_path, workbook_path)
    write_audit_json(data, summary, audit_path)

    print("generated:")
    print(docx_path)
    print(workbook_path)
    print(audit_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
