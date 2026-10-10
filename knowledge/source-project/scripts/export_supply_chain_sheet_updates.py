from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = ROOT / "deploy" / "lingshi_dashboard_report"
if str(DEPLOY_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(DEPLOY_ROOT / "scripts"))

import generate_lingshi_dashboard as dashboard  # noqa: E402


DEFAULT_TEMPLATE = DEPLOY_ROOT / "templates" / "lingshi_dashboard_template.xlsx"
DEFAULT_ENV_FILE = DEPLOY_ROOT / "config" / "pickup.env"
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "supply_chain_sheet_updates"

SUPPLY_HEADERS = ["SKU编码", "SKU名称", "实收数量", "创建时间", "月份"]
INVENTORY_SHEET_HEADERS = [
    "wms_desc",
    "SKU编码",
    "SKU名称",
    "可用数量",
    "汇总数量",
    "在库数量",
    "在途数量",
    "分配数量",
    "今日总出库数",
    "昨日总出库数",
    "7日内总出库数",
    "近七天平均出库数",
    "库存可用天数",
    "锁定数量",
    "冻结数量",
    "业务中台占用数量",
    "OFS占有用数量",
    "采购在途",
    "调拨在途",
]


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def tsv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y/%-m/%-d") if sys.platform != "win32" else f"{value.year}/{value.month}/{value.day}"
    if isinstance(value, date):
        return f"{value.year}/{value.month}/{value.day}"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    return str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()


def write_tsv(path: Path, rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, delimiter="\t", lineterminator="\n")
        for row in rows:
            writer.writerow([tsv_value(value) for value in row])


def fetch_supply_purchase_rows(connection, start: date, end_exclusive: date) -> list[list[Any]]:
    sql = """
        SELECT
            d.sku_code,
            COALESCE(ms.sku_name, d.sku_name) AS sku_name,
            SUM(IFNULL(d.received_quantity, 0)) AS received_qty,
            DATE(d.created_date) AS created_date,
            CONCAT(MONTH(DATE(d.created_date)), '月') AS month_label
        FROM scm_warehous_header h
        INNER JOIN scm_warehous_detail d
            ON d.warehous_code = h.warehous_code
        LEFT JOIN master_sku ms
            ON ms.sku_code = d.sku_code
        WHERE h.cargo_owner_id = 'HJXH'
          AND h.warehous_type = '10'
          AND h.status = '0'
          AND d.status = '0'
          AND h.warehous_status = '15'
          AND d.created_date >= %s
          AND d.created_date < %s
          AND IFNULL(d.received_quantity, 0) <> 0
        GROUP BY
            d.sku_code,
            COALESCE(ms.sku_name, d.sku_name),
            DATE(d.created_date)
        ORDER BY
            DATE(d.created_date),
            d.sku_code
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, (start, end_exclusive))
        rows = list(cursor.fetchall())
    return [
        [
            row.get("sku_code"),
            row.get("sku_name"),
            dashboard.ceil_non_negative(row.get("received_qty")),
            row.get("created_date"),
            row.get("month_label"),
        ]
        for row in rows
    ]


def fetch_inventory_rows(connection) -> tuple[list[list[Any]], dict[str, Any]]:
    sql = """
        SELECT
            mwms.wms_desc AS wms_desc,
            tall.sku_code AS sku_code,
            ms.sku_name AS sku_name,
            GREATEST(
                IFNULL(w.in_stock_quantity, 0)
              - IFNULL(allocate.allocate_qty, 0)
              - CASE WHEN IFNULL(w.stock_lock, 0) = 0 THEN IFNULL(w.freeze_qty, 0) ELSE IFNULL(w.stock_lock, 0) END,
                0
            ) AS available_qty,
            IFNULL(w.in_stock_quantity, 0)
              - IFNULL(w.stock_lock, 0)
              - IFNULL(allocate.allocate_qty, 0)
              + IFNULL(po.po_in_transit, 0)
              + IFNULL(tr.tr_in_transit, 0) AS summary_qty,
            IFNULL(w.in_stock_quantity, 0) AS in_stock_qty,
            IFNULL(po.po_in_transit, 0) + IFNULL(tr.tr_in_transit, 0) AS transit_qty,
            IFNULL(allocate.allocate_qty, 0) AS allocate_qty,
            IFNULL(sumq.shipQty1, 0) AS today_ship_qty,
            IFNULL(sumq.shipQty2, 0) AS yesterday_ship_qty,
            IFNULL(sumq.shipQty3, 0) AS seven_day_ship_qty,
            ROUND(IFNULL(sumq.shipQty3, 0) / 7, 2) AS seven_day_avg_ship_qty,
            ROUND(
                (
                    IFNULL(w.in_stock_quantity, 0)
                  - IFNULL(w.stock_lock, 0)
                  - IFNULL(allocate.allocate_qty, 0)
                ) / (sumq.shipQty3 / 7),
                2
            ) AS inventory_available_days,
            IFNULL(w.stock_lock, 0) AS stock_lock_qty,
            IFNULL(w.freeze_qty, 0) AS freeze_qty,
            IFNULL(allocate.allocate_qty, 0) AS business_occupy_qty,
            0 AS ofs_occupy_qty,
            IFNULL(po.po_in_transit, 0) AS po_in_transit,
            IFNULL(tr.tr_in_transit, 0) AS transfer_in_transit
        FROM (
            SELECT DISTINCT tot.wms_code, tot.cargo_owner_id, tot.sku_code
            FROM (
                SELECT p.take_delivery_wms AS wms_code,
                       p.cargo_owner_id AS cargo_owner_id,
                       d.sku_code
                FROM ywzt_ods.scm_po_header p
                LEFT JOIN ywzt_ods.scm_po_detail d
                    ON p.po_code = d.po_code
                WHERE p.order_status IN ('10','11','12','13','14','15','16')
                UNION
                SELECT scm.wms_code, scm.cargo_owner_id, scm.sku_code
                FROM ywzt_ods.scm_warehous_manage scm
                UNION
                SELECT wms_code, cargo_owner_id, sku_code
                FROM ywzt_ods.scm_stock_history
                UNION
                SELECT p.receive_wms AS wms_code,
                       p.receiver_cargo_owner AS cargo_owner_id,
                       d.sku_code
                FROM ywzt_ods.scm_transfer_order_header p
                LEFT JOIN ywzt_ods.scm_transfer_order_detail d
                    ON p.transfer_order_code = d.transfer_order_code
                WHERE p.order_status IN ('13', '14', '15')
            ) tot
        ) tall
        LEFT JOIN (
            SELECT
                wms_code,
                cargo_owner_id,
                sku_code,
                sku_name,
                SUM(IFNULL(in_stock_quantity, 0)) AS in_stock_quantity,
                SUM(
                    CASE
                        WHEN stock_lock IS NULL OR stock_lock = '' THEN 0
                        ELSE CAST(stock_lock AS DECIMAL(20,4))
                    END
                ) AS stock_lock,
                SUM(IFNULL(CAST(freeze_quantity AS DECIMAL(20,4)), 0)) AS freeze_qty
            FROM ywzt_ods.scm_warehous_manage w
            WHERE (w.inventory_status = 'ZP' OR w.inventory_status IS NULL OR w.inventory_status = '')
            GROUP BY cargo_owner_id, wms_code, sku_code, sku_name
        ) w
            ON tall.wms_code = w.wms_code
           AND tall.cargo_owner_id = w.cargo_owner_id
           AND tall.sku_code = w.sku_code
        LEFT JOIN (
            SELECT wms_code, cargo_owner_id, sku_code, SUM(IFNULL(freeze_quantity, 0)) AS allocate_qty
            FROM ywzt_ods.scm_warehous_occupy
            WHERE created_date > DATE_SUB(NOW(), INTERVAL 60 DAY)
            GROUP BY cargo_owner_id, sku_code, wms_code
        ) allocate
            ON tall.cargo_owner_id = allocate.cargo_owner_id
           AND tall.sku_code = allocate.sku_code
           AND tall.wms_code = allocate.wms_code
        LEFT JOIN (
            SELECT p.take_delivery_wms AS wms_code,
                   p.cargo_owner_id,
                   d.sku_code,
                   SUM(IFNULL(d.request_quantity, 0)) AS po_in_transit
            FROM ywzt_ods.scm_po_header p
            LEFT JOIN ywzt_ods.scm_po_detail d
                ON p.po_code = d.po_code
            WHERE p.order_status IN ('12','13','14','15','16')
              AND d.status = 0
            GROUP BY p.cargo_owner_id, p.take_delivery_wms, d.sku_code
        ) po
            ON tall.cargo_owner_id = po.cargo_owner_id
           AND tall.wms_code = po.wms_code
           AND tall.sku_code = po.sku_code
        LEFT JOIN (
            SELECT d.cargo_owner_id AS cargo_owner_id,
                   p.receive_wms AS wms_code,
                   d.sku_code,
                   SUM(IFNULL(d.allocate_qty, 0)) AS tr_in_transit
            FROM ywzt_ods.scm_transfer_order_header p
            LEFT JOIN ywzt_ods.scm_sales_shipment_allocate_wms d
                ON p.outbound_code = d.outbound_code
            WHERE p.order_status IN ('13', '14', '15')
            GROUP BY d.cargo_owner_id, p.receive_wms, d.sku_code
        ) tr
            ON tall.cargo_owner_id = tr.cargo_owner_id
           AND tall.sku_code = tr.sku_code
           AND tall.wms_code = tr.wms_code
        LEFT JOIN (
            SELECT swms.sku_code,
                   scm_h.company_code,
                   swms.wms_code,
                   SUM(IF(DATE_FORMAT(scm_h.ship_at, '%Y-%m-%d') = DATE_FORMAT(NOW(), '%Y-%m-%d'), IFNULL(swms.allocate_qty, 0), 0)) AS shipQty1,
                   SUM(IF(DATE_FORMAT(scm_h.ship_at, '%Y-%m-%d') = DATE_SUB(CURDATE(), INTERVAL 1 DAY), IFNULL(swms.allocate_qty, 0), 0)) AS shipQty2,
                   SUM(IFNULL(swms.allocate_qty, 0)) AS shipQty3
            FROM ywzt_ods.scm_sales_shipment_header scm_h
            LEFT JOIN ywzt_ods.scm_sales_shipment_allocate_wms swms
                ON scm_h.outbound_code = swms.outbound_code
            WHERE scm_h.status IN (700, 900)
              AND scm_h.ship_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)
              AND scm_h.ref_order_type = 'JYCK'
            GROUP BY swms.sku_code, scm_h.company_code, swms.wms_code
        ) sumq
            ON tall.wms_code = sumq.wms_code
           AND tall.cargo_owner_id = sumq.company_code
           AND tall.sku_code = sumq.sku_code
        LEFT JOIN ywzt_ods.master_wms mwms
            ON tall.wms_code = mwms.wms_code
        LEFT JOIN ywzt_ods.master_sku ms
            ON ms.sku_code = tall.sku_code
        WHERE ms.group_number = 'CHJZFL05-SYS'
          AND tall.cargo_owner_id = 'HJXH'
          AND mwms.wms_type IN (10, 11)
          AND mwms.wms_desc NOT LIKE '%残次%'
        ORDER BY
            mwms.wms_desc,
            tall.sku_code
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        rows = list(cursor.fetchall())

    output_rows = [[row.get(field) for field in (
        "wms_desc",
        "sku_code",
        "sku_name",
        "available_qty",
        "summary_qty",
        "in_stock_qty",
        "transit_qty",
        "allocate_qty",
        "today_ship_qty",
        "yesterday_ship_qty",
        "seven_day_ship_qty",
        "seven_day_avg_ship_qty",
        "inventory_available_days",
        "stock_lock_qty",
        "freeze_qty",
        "business_occupy_qty",
        "ofs_occupy_qty",
        "po_in_transit",
        "transfer_in_transit",
    )] for row in rows]
    audit = {
        "inventory_row_count": len(output_rows),
        "inventory_source": "ywzt_ods.scm_warehous_manage + scm_warehous_occupy + PO/transfer transit + shipment allocation",
        "inventory_headers": INVENTORY_SHEET_HEADERS,
    }
    return output_rows, audit


def main() -> int:
    parser = argparse.ArgumentParser(description="导出腾讯文档供应链日报更新用 TSV")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--run-date", type=parse_date, default=date.today())
    parser.add_argument("--supply-start", type=parse_date, required=True)
    parser.add_argument("--supply-end", type=parse_date, default=date.today())
    args = parser.parse_args()

    dashboard.load_env_file(args.env_file)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with dashboard.connect() as connection:
        inventory_rows, inventory_audit = fetch_inventory_rows(connection)
        supply_rows = fetch_supply_purchase_rows(
            connection,
            args.supply_start,
            args.supply_end + timedelta(days=1),
        )

    date_label = args.run_date.strftime("%Y%m%d")
    inventory_path = args.output_dir / f"当日库存_覆盖_{date_label}.tsv"
    supply_path = args.output_dir / f"供销数据源_追加_{args.supply_start:%Y%m%d}_{args.supply_end:%Y%m%d}.tsv"
    audit_path = args.output_dir / f"腾讯文档更新审计_{date_label}.json"

    write_tsv(inventory_path, inventory_rows)
    write_tsv(supply_path, supply_rows)
    audit = {
        "run_date": args.run_date.isoformat(),
        "inventory_tsv": str(inventory_path),
        "inventory_rows": len(inventory_rows),
        "inventory_columns": len(INVENTORY_SHEET_HEADERS),
        "inventory_headers": INVENTORY_SHEET_HEADERS,
        "inventory": inventory_audit,
        "supply_tsv": str(supply_path),
        "supply_rows": len(supply_rows),
        "supply_columns": len(SUPPLY_HEADERS),
        "supply_headers": SUPPLY_HEADERS,
        "supply_start": args.supply_start.isoformat(),
        "supply_end": args.supply_end.isoformat(),
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
