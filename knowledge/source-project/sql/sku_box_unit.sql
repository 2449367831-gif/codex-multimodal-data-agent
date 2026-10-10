/* SKU 箱规
   unit = 40 表示箱。
   后续所有展示数量均需换算为箱数：
   box_qty = ea_qty / ea_convert_quantity
*/
SELECT
    sku_code,
    sku_name,
    ea_convert_quantity
FROM master_sku_unit
WHERE unit = 40
  AND IFNULL(ea_convert_quantity, 0) > 0;
