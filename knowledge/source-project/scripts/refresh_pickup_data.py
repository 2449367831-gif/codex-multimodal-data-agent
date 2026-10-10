from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQL_DIR = ROOT / "sql"
DEFAULT_OUTPUT = ROOT / "data" / "pickup-data.json"


STATUS_BULK = "bulk"
STATUS_NORMAL = "normal"
STATUS_LIMITED = "limited"
STATUS_NO_SALES = "no-sales"


def load_env_file(path: Path | None) -> None:
    if not path:
        return
    if not path.exists():
        raise FileNotFoundError(f"env file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_sql(sql_dir: Path, name: str) -> str:
    sql = (sql_dir / name).read_text(encoding="utf-8").strip()
    if sql.endswith(";"):
        sql = sql[:-1]
    return sql


def to_number(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def floor_non_negative(value) -> int:
    number = to_number(value)
    if not math.isfinite(number) or number <= 0:
        return 0
    return math.floor(number)


def box_qty(ea_qty, box_unit: float | None) -> int:
    if not box_unit or box_unit <= 0:
        return 0
    return floor_non_negative(to_number(ea_qty) / box_unit)


def row_key(row: dict) -> tuple[str, str]:
    return str(row.get("warehouse_code") or "").strip(), str(row.get("sku_code") or "").strip()


def status_for(pickup_box_qty: int, offline_avg_7d_box_qty: float) -> tuple[str, int | None]:
    if offline_avg_7d_box_qty <= 0:
        return STATUS_NO_SALES, None
    inventory_days = math.floor(pickup_box_qty / offline_avg_7d_box_qty)
    if inventory_days < 7:
        return STATUS_LIMITED, inventory_days
    if inventory_days < 15:
        return STATUS_NORMAL, inventory_days
    return STATUS_BULK, inventory_days


def connect():
    try:
        import pymysql
    except ImportError as exc:
        raise SystemExit("缺少依赖 PyMySQL，请先执行：py -m pip install -r requirements.txt") from exc

    host = os.environ.get("PICKUP_DB_HOST", "").strip()
    user = os.environ.get("PICKUP_DB_USER", "").strip()
    password = os.environ.get("PICKUP_DB_PASSWORD", "")
    database = os.environ.get("PICKUP_DB_NAME", "ywzt_ods").strip() or "ywzt_ods"
    port = int(os.environ.get("PICKUP_DB_PORT", "9030"))

    missing = [name for name, value in {
        "PICKUP_DB_HOST": host,
        "PICKUP_DB_USER": user,
        "PICKUP_DB_PASSWORD": password,
    }.items() if not value]
    if missing:
        raise SystemExit(f"缺少数据库环境变量：{', '.join(missing)}")

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
        read_timeout=180,
        write_timeout=60,
    )


def fetch_all(connection, sql: str) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def index_by_key(rows: list[dict]) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = row_key(row)
        if key[0] and key[1]:
            result[key] = row
    return result


def build_payload(
    realtime_rows: list[dict],
    third_party_recent_rows: list[dict],
    sales_rows: list[dict],
    box_rows: list[dict],
    sku_scope_rows: list[dict],
) -> dict:
    sku_scope = {
        str(row.get("sku_code") or "").strip(): str(row.get("sku_name") or "").strip()
        for row in sku_scope_rows
        if str(row.get("sku_code") or "").strip()
    }
    realtime_by_key = index_by_key(realtime_rows)
    third_recent_by_key = index_by_key(third_party_recent_rows)
    sales_by_key = index_by_key(sales_rows)

    box_units: dict[str, float] = {}
    box_names: dict[str, str] = {}
    for row in box_rows:
        sku_code = str(row.get("sku_code") or "").strip()
        unit = to_number(row.get("ea_convert_quantity"))
        if sku_code and unit > 0:
            box_units[sku_code] = unit
            box_names[sku_code] = str(row.get("sku_name") or "").strip()

    keys = set(realtime_by_key) | set(third_recent_by_key) | set(sales_by_key)
    output_rows: list[dict] = []

    for key in sorted(keys, key=lambda item: (item[0], item[1])):
        warehouse_code, sku_code = key
        if sku_scope and sku_code not in sku_scope:
            continue

        realtime = realtime_by_key.get(key, {})
        third_recent = third_recent_by_key.get(key)
        sales = sales_by_key.get(key, {})
        source_recent = third_recent if third_recent is not None else realtime

        warehouse_name = (
            realtime.get("warehouse_name")
            or (third_recent or {}).get("warehouse_name")
            or sales.get("warehouse_name")
            or warehouse_code
        )
        sku_name = (
            sku_scope.get(sku_code)
            or realtime.get("sku_name")
            or (third_recent or {}).get("sku_name")
            or sales.get("sku_name")
            or box_names.get(sku_code)
            or sku_code
        )

        box_unit = box_units.get(sku_code)
        has_box_unit = bool(box_unit and box_unit > 0)

        stock_box = box_qty(realtime.get("stock_qty"), box_unit)
        recent_stock_box = box_qty((source_recent or {}).get("recent_stock_qty"), box_unit)
        online_sales_7d_box = box_qty(sales.get("online_reserved_7d_qty"), box_unit)
        offline_sales_7d_box_float = to_number(sales.get("offline_sales_7d_qty")) / box_unit if has_box_unit else 0.0
        offline_avg_7d_box = offline_sales_7d_box_float / 7 if has_box_unit else 0.0
        pickup_box = min(recent_stock_box, max(stock_box - online_sales_7d_box, 0)) if has_box_unit else 0
        status, inventory_days = status_for(pickup_box, offline_avg_7d_box)

        output_rows.append({
            "warehouse_code": warehouse_code,
            "warehouse_name": str(warehouse_name or ""),
            "sku_code": sku_code,
            "sku_name": str(sku_name or ""),
            "ea_convert_quantity": box_unit or 0,
            "box_unit_missing": not has_box_unit,
            "stock_box_qty": stock_box,
            "recent_stock_box_qty": recent_stock_box,
            "online_sales_7d_box_qty": online_sales_7d_box,
            "offline_sales_7d_box_qty": floor_non_negative(offline_sales_7d_box_float),
            "offline_avg_7d_box_qty": offline_avg_7d_box,
            "offline_pickup_box_qty": pickup_box,
            "inventory_days": inventory_days,
            "status": status,
            "stock_source": "third_party_60d" if third_recent is not None else "realtime_fallback",
        })

    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "updated_at": now,
        "note": f"数仓脚本刷新成功，共 {len(output_rows)} 条数据",
        "rows": output_rows,
    }


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    last_error: Exception | None = None
    for attempt in range(12):
        try:
            tmp_path.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))

    backup_path = path.with_suffix(path.suffix + f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
    try:
        if path.exists():
            path.rename(backup_path)
        tmp_path.replace(path)
        return
    except PermissionError as exc:
        last_error = exc

    raise PermissionError(f"failed to replace {path} after retries: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新线下可提货数据 JSON")
    parser.add_argument("--env-file", type=Path, default=None, help="环境变量文件，例如 config/pickup.env")
    parser.add_argument("--sql-dir", type=Path, default=DEFAULT_SQL_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="只取数并打印摘要，不写文件")
    args = parser.parse_args()

    load_env_file(args.env_file)
    connection = connect()
    try:
        sql_dir = args.sql_dir
        realtime_rows = fetch_all(connection, read_sql(sql_dir, "realtime_stock.sql"))
        third_party_recent_rows = fetch_all(connection, read_sql(sql_dir, "third_party_stock_60d.sql"))
        sales_rows = fetch_all(connection, read_sql(sql_dir, "offline_sales_7d_warehouse_sku.sql"))
        box_rows = fetch_all(connection, read_sql(sql_dir, "sku_box_unit.sql"))
        sku_scope_rows = fetch_all(connection, "SELECT sku_code, sku_name FROM master_sku WHERE sku_group = '50'")
    finally:
        connection.close()

    payload = build_payload(realtime_rows, third_party_recent_rows, sales_rows, box_rows, sku_scope_rows)

    if args.dry_run:
        print(json.dumps({
            "updated_at": payload["updated_at"],
            "row_count": len(payload["rows"]),
            "missing_box_unit_count": sum(1 for row in payload["rows"] if row.get("box_unit_missing")),
            "output": str(args.output),
        }, ensure_ascii=False, indent=2))
        return 0

    atomic_write_json(args.output, payload)
    print(json.dumps({
        "ok": True,
        "updated_at": payload["updated_at"],
        "row_count": len(payload["rows"]),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
