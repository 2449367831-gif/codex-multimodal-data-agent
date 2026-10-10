# 线下提货指导表口径

## 已确认规则

- 页面名称：线下可提货数据。
- 页面做成 HTML，通过公司域名访问。
- 数据不在浏览器直连数仓，后续由服务器脚本每 2 小时刷新一次数据文件。
- 线下销售可提货只看生产日期在近 60 天以内的库存，边界按当前日期往前推 60 天，不按自然月。
- 线上 7 天预留库存不限制生产日期；两个月以上库存也可以留给线上销售消化。
- 含生产日期的库存只有自有仓。
- 三方仓近 60 天库存有单独取数 SQL；三方仓 SQL 按 `仓库 + SKU` 有数据时，自动排除实时库存 SQL 中相同 `仓库 + SKU` 的数据。
- 三方仓 SQL 按 `仓库 + SKU` 没有数据时，使用实时库存 SQL 的对应数据兜底。
- 线下可提货数量需要先预留线上 7 天库存。
- 线上预留扣减方式先按可用库存计算：`线上预留后剩余库存 = 可用库存数量（不限制生产日期） - 线上近 7 天销量`。
- 线下可提货数量不能超过近 60 天生产库存：`线下可提货数量 = min(近 60 天生产库存, max(线上预留后剩余库存, 0))`。
- 线下可提货数量不允许为负数，不允许小数；库存不够扣时置 0。
- “辅料、非近 60 天生产库存不显示数量”仅作为业务提示文案，不作为系统筛选或计算规则。
- 库存天数 = 线下可提货数量 / 线下近 7 天日均销售，不保留小数。
- 近 7 天线下日均销售为 0 时，状态展示“近 7 天线下无动销”，库存天数展示 `--`。
- 页面所有数量必须展示为箱数，不展示 EA/个数。
- 箱规取数：`SELECT ea_convert_quantity, sku_name, sku_code FROM master_sku_unit WHERE unit = 40`。
- 箱数换算：`箱数 = EA数量 / ea_convert_quantity`；展示箱数不允许小数时向下取整。
- 如果 SKU 没有箱规或箱规为 0，页面显示“无箱规”。
- SKU/仓库展示范围：最终展示所有 `master_sku.sku_group = '50'` 的 SKU。

## 近 7 天出库 SQL 口径

- 时间范围：`h.ship_at >= CURDATE() - INTERVAL 7 DAY AND h.ship_at < CURDATE()`。
- 出库单号：`h.outbound_code LIKE 'HJXH%'`。
- 状态：`h.status IN ('700', '900', '701', '901')`。
- 出库类型：`h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK') OR h.ref_order_type LIKE 'SO%'`。
- 线上 7 天预留库存：`SUM(CASE WHEN s.type = 'Z004' THEN i.ship_qty ELSE 0 END)`。
- 线下近 7 天销量：`线下社区团购_平台 + 线下社区团购_分销 + 线下其它`。
- 线下近 7 天日均销售：`线下近 7 天销量 / 7`。
- 上述销量字段需按 `master_sku_unit.ea_convert_quantity` 换算为箱数后再展示和计算。

## 近 60 天库存 SQL 口径

- 表：`scm_warehous_manage_3d`。
- 这是三方仓近 60 天生产库存优先来源。
- 库存快照：取 `scm_warehous_manage_3d` 最近有数据的 `created_date` 日期，当日全天数据。
- 生产日期：`wm.production_date > CURDATE() - INTERVAL 60 DAY`。
- 货主：`wm.cargo_owner_id = 'HJXH'`。
- 商品范围：`master_sku.sku_group = '50'`。
- 库存数量：`SUM(wm.in_stock_quantity)`。
- 库存数量需按 `master_sku_unit.ea_convert_quantity` 换算为箱数后再展示和计算。
- 输出粒度：`仓库 + SKU`。
- 原始 SQL 已保存在 `sql/recent_stock_60d_original.sql`。
- 整合用 SQL 已整理为 `sql/recent_stock_60d.sql`。
- 三方仓原始 SQL 已保存在 `sql/third_party_stock_60d_original.sql`。
- 三方仓整合用 SQL 已整理为 `sql/third_party_stock_60d.sql`。

## 实时库存 SQL 口径

- 原始 SQL 已保存在 `sql/realtime_stock_original.sql`。
- 整合用 SQL 已整理为 `sql/realtime_stock.sql`。
- 表：`scm_warehous_manage`。
- 货主：`HJXH`。
- 库存状态：`inventory_status = 'ZP' OR inventory_status IS NULL OR inventory_status = ''`。
- 仓库：`master_wms.wms_type IN (10, 11)`，排除仓库名称包含“残次”的仓。
- 商品：`master_sku.sku_group = '50'`。
- 所有库存：使用“可用数量”口径，扣分配、锁定/冻结后不小于 0。
- 近 60 天生产库存：`SUM(IF(production_date > CURDATE() - INTERVAL 60 DAY, in_stock_quantity, 0))`。
- 所有库存、近 60 天生产库存需按 `master_sku_unit.ea_convert_quantity` 换算为箱数后再展示和计算。

## 箱规 SQL 口径

- 表：`master_sku_unit`。
- 条件：`unit = 40`。
- 字段：`sku_code`、`sku_name`、`ea_convert_quantity`。
- 整合用 SQL 已整理为 `sql/sku_box_unit.sql`。
- 无箱规或箱规为 0 的 SKU 不参与箱数换算，页面显示“无箱规”。

## 状态展示

- 线下近 7 天日均销售为 0：近 7 天线下无动销，蓝色。
- 库存天数小于 7：不建议大量提货，红色。
- 库存天数大于等于 7 且小于 15：正常销售，黄色。
- 库存天数大于等于 15：可大量提货，绿色。
