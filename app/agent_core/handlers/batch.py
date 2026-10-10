"BatchInventoryHandler SQL and result reconciliation."

from __future__ import annotations
from datetime import date, timedelta
from decimal import Decimal
from ..semantic_types import (
    SemanticAnswer,
    SemanticRequest,
    SemanticRuntimeError,
    _date_label,
    _display_table,
    _parse_decimal,
    _parse_source_datetime,
)
from .base import MetricHandler


def _count(row, key):
    value = row[key]
    if isinstance(value, bool):
        raise ValueError("invalid batch count")
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError("invalid batch count")
    return int(number)


def _measure(row, prefix, name, row_count):
    value_count = _count(row, f"{prefix}_{name}_value_count")
    if value_count > row_count:
        raise ValueError("batch value count exceeds source rows")
    raw = row[f"{prefix}_{name}_ea"]
    value = _parse_decimal(raw) if raw is not None else None
    if value is not None and value < 0:
        raise ValueError("negative batch inventory")
    if value_count == 0 and value not in (None, Decimal(0)):
        raise ValueError("batch quantity without values")
    if value_count > 0 and value is None:
        raise ValueError("batch sum missing")
    # A partial SUM ignores NULL rows; it is not a complete stock quantity.
    return value if row_count > 0 and value_count == row_count else None


def _quantity(value):
    return format(value, "f") if value is not None else None


class BatchInventoryHandler(MetricHandler):
    def _batch_sql(self, production_date: date, *, product_scope: str = "sausage") -> str:
        start_text = self._sql_date(production_date)
        end_text = self._sql_date(production_date + timedelta(days=1))
        owner = self.owner.replace("'", "''")
        wms = self.changsha_huangxing_wms
        if product_scope == "finished_goods":
            sku_group_filter = "TRIM(sku_group) = '50'"
            sku_name_filter = "1 = 1"
        elif product_scope == "all_products":
            # An omitted product scope means ALL.  Do not silently inherit the
            # finished-goods group used by the sausage shortcut.
            sku_group_filter = "1 = 1"
            sku_name_filter = "1 = 1"
        elif product_scope == "sausage":
            sku_group_filter = "TRIM(sku_group) = '50'"
            sku_name_filter = "sku_name LIKE '%烤肠%' OR sku_name LIKE '%香肠%' OR sku_name LIKE '%脆皮肠%'"
        else:
            raise ValueError("unsupported batch product scope")
        return f"""WITH sausage_skus AS (
  SELECT sku_code
  FROM (
    SELECT TRIM(sku_code) AS sku_code,
           ROW_NUMBER() OVER (
             PARTITION BY TRIM(sku_code)
             ORDER BY updated_date DESC, version DESC, id DESC
           ) AS rn
    FROM ywzt_ods.master_sku
    WHERE {sku_group_filter}
      AND COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
      AND ({sku_name_filter})
  ) AS ranked_sku
  WHERE rn = 1
), current_total AS (
  SELECT COUNT(*) AS current_row_count,
         COUNT(DISTINCT TRIM(i.sku_code)) AS current_sku_count,
         COUNT(i.in_stock_quantity) AS current_stock_value_count,
         COUNT(i.actual_quantity) AS current_actual_value_count,
         SUM(i.in_stock_quantity) AS current_stock_ea,
         SUM(i.actual_quantity) AS current_actual_ea,
         MAX(i.stock_change_time) AS current_freshness
  FROM ywzt_ods.scm_warehous_manage AS i
  JOIN sausage_skus AS s ON s.sku_code = TRIM(i.sku_code)
  WHERE TRIM(CAST(i.cargo_owner_id AS STRING)) = '{owner}'
    AND TRIM(i.wms_code) = '{wms}'
    AND i.production_date >= '{start_text} 00:00:00'
    AND i.production_date < '{end_text} 00:00:00'
    AND COALESCE(TRIM(CAST(i.status AS STRING)), '0') = '0'
    AND COALESCE(TRIM(CAST(i.inventory_status AS STRING)), '') IN ('', 'ZP')
), backup_dates AS (
  SELECT MAX(b.backups_date) AS latest_backup_date
  FROM ywzt_ods.scm_warehous_manage_backups AS b
  JOIN sausage_skus AS s ON s.sku_code = TRIM(b.sku_code)
  WHERE TRIM(CAST(b.cargo_owner_id AS STRING)) = '{owner}'
    AND TRIM(b.wms_code) = '{wms}'
    AND b.production_date >= '{start_text} 00:00:00'
    AND b.production_date < '{end_text} 00:00:00'
    AND COALESCE(TRIM(CAST(b.status AS STRING)), '0') = '0'
    AND COALESCE(TRIM(CAST(b.inventory_status AS STRING)), '') IN ('', 'ZP')
    AND IFNULL(b._fdl_marked_deleted, 'false') = 'false'
), backup_total AS (
  SELECT d.latest_backup_date,
         COUNT(b.sku_code) AS backup_row_count,
         COUNT(DISTINCT TRIM(b.sku_code)) AS backup_sku_count,
         COUNT(b.in_stock_quantity) AS backup_stock_value_count,
         COUNT(b.actual_quantity) AS backup_actual_value_count,
         SUM(b.in_stock_quantity) AS backup_stock_ea,
         SUM(b.actual_quantity) AS backup_actual_ea
  FROM backup_dates AS d
  LEFT JOIN ywzt_ods.scm_warehous_manage_backups AS b
    ON b.backups_date = d.latest_backup_date
   AND TRIM(CAST(b.cargo_owner_id AS STRING)) = '{owner}'
   AND TRIM(b.wms_code) = '{wms}'
   AND b.production_date >= '{start_text} 00:00:00'
   AND b.production_date < '{end_text} 00:00:00'
   AND COALESCE(TRIM(CAST(b.status AS STRING)), '0') = '0'
   AND COALESCE(TRIM(CAST(b.inventory_status AS STRING)), '') IN ('', 'ZP')
   AND IFNULL(b._fdl_marked_deleted, 'false') = 'false'
   AND TRIM(b.sku_code) IN (SELECT sku_code FROM sausage_skus)
  GROUP BY d.latest_backup_date
)
SELECT c.current_row_count,
       c.current_sku_count,
       c.current_stock_value_count,
       c.current_actual_value_count,
       c.current_stock_ea,
       c.current_actual_ea,
       c.current_freshness,
       b.latest_backup_date,
       b.backup_row_count,
       b.backup_sku_count,
       b.backup_stock_value_count,
       b.backup_actual_value_count,
       b.backup_stock_ea,
       b.backup_actual_ea
FROM current_total AS c
CROSS JOIN backup_total AS b"""

    def _run_batch_inventory(self, request: SemanticRequest) -> SemanticAnswer:
        assert request.production_date is not None
        product_scope = request.product_keyword or "sausage"
        if product_scope not in {"sausage", "finished_goods", "all_products"}:
            raise SemanticRuntimeError("生产日期库存范围无法确认，暂未展示结果", reason="batch_scope_invalid")
        product_label = {
            "sausage": "烤肠",
            "finished_goods": "成品",
            "all_products": "全部商品",
        }[product_scope]
        rows = self._execute(
            self._batch_sql(request.production_date, product_scope=product_scope),
            purpose=("production-date finished-goods inventory at Changsha Huangxing"
                     if product_scope == "finished_goods"
                     else "production-date all-product inventory at Changsha Huangxing"
                     if product_scope == "all_products"
                     else "production-date sausage inventory at Changsha Huangxing"),
        )
        if len(rows) != 1:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="batch_scalar_invalid")
        row = rows[0]
        try:
            current_count = _count(row, "current_row_count")
            backup_count = _count(row, "backup_row_count")
            for prefix, count in (("current", current_count), ("backup", backup_count)):
                sku_count = _count(row, f"{prefix}_sku_count")
                if sku_count > count or (sku_count == 0) != (count == 0):
                    raise ValueError("invalid batch SKU count")
            current_stock = _measure(row, "current", "stock", current_count)
            current_actual = _measure(row, "current", "actual", current_count)
            backup_stock = _measure(row, "backup", "stock", backup_count)
            _measure(row, "backup", "actual", backup_count)
            latest = row["latest_backup_date"]
            latest_date = date.fromisoformat(str(latest)[:10]) if latest is not None else None
            if (latest_date is None) != (backup_count == 0):
                raise ValueError("inconsistent batch snapshot")
            freshness = row["current_freshness"]
            if freshness is not None:
                freshness = _parse_source_datetime(freshness, timezone_name=self.timezone_name)
            if current_count == 0 and freshness is not None:
                raise ValueError("batch freshness without source rows")
        except (ValueError, TypeError, KeyError) as exc:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="batch_values_invalid") from exc
        label = _date_label(request.production_date)
        latest_text = latest_date.isoformat() if latest_date is not None else "无"
        answer = f"{label}生产的{product_label}，在长沙黄兴"
        if not current_count:
            answer += "当前没有匹配库存记录，不能据此确认库存为零。"
        elif current_stock is None:
            answer += "库存数量存在缺失，当前库存无法完整确认。"
        else:
            answer += f"当前库存：{_quantity(current_stock)} EA。"
        if current_count:
            answer += (f"当前实际库存：{_quantity(current_actual)} EA。" if current_actual is not None
                       else "实际库存数量存在缺失，未按零值展示。")
            answer += (f"库存最近变动：{freshness.strftime('%Y-%m-%d %H:%M:%S')}；不代表完整同步时点。"
                       if freshness is not None else "库存变动时间缺失，鲜度无法确认。")
        if latest_date is not None:
            answer += (f"历史快照（{latest_text}）：{_quantity(backup_stock)} EA，仅作历史参考，不替代当前库存。"
                       if backup_stock is not None else f"历史快照（{latest_text}）库存数量存在缺失，未按零值展示。")
        else:
            answer += "没有匹配的历史快照，未将缺失数据视为零库存。"
        table = _display_table(
            table_id="batch_inventory",
            title="生产批次库存",
            caption=f"{label} · 长沙黄兴 · {product_label}",
            columns=[
                {"key": "production_date", "kind": "text"},
                {"key": "warehouse", "kind": "text"},
                {"key": "current_stock", "kind": "number", "unit": "EA"},
                {"key": "available_stock", "kind": "number", "unit": "EA"},
                {"key": "snapshot_stock", "kind": "number", "unit": "EA"},
                {"key": "snapshot_date", "kind": "text"},
            ],
            rows=[
                {
                    "production_date": label,
                    "warehouse": "长沙黄兴",
                    "current_stock": _quantity(current_stock),
                    "available_stock": _quantity(current_actual),
                    "snapshot_stock": _quantity(backup_stock),
                    "snapshot_date": latest_text,
                }
            ] if current_count or backup_count else [],
            summary=[
                {"label": title, "value": _quantity(value), "unit": "EA"}
                for title, value in (("当前库存", current_stock), ("当前实际库存", current_actual))
                if value is not None
            ],
        )
        return SemanticAnswer(answer, table, result_state="no_data" if not current_count else "live_success")
