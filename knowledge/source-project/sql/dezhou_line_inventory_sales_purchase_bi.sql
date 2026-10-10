WITH
params AS (
    SELECT
        CAST(DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') AS DATE) AS range_start_date,
        CAST(DATE_FORMAT(CURDATE(), '%Y-%m-01') AS DATE) AS month_start_date,
        DATE_SUB(CURDATE(), INTERVAL 1 DAY) AS yesterday_date,
        CURDATE() AS range_end_exclusive
),
sku_label AS (
    SELECT '5001010011120600' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（原味）0600G12' AS product_name
    UNION ALL
    SELECT '5001010011120800G' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（原味）800g12' AS product_name
    UNION ALL
    SELECT '5001010011121000' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（原味）双包1000G12' AS product_name
    UNION ALL
    SELECT '5001010012120600' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（黑椒味）0600G12' AS product_name
    UNION ALL
    SELECT '5001010012120800G' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（黑椒味）800g12' AS product_name
    UNION ALL
    SELECT '5001010012121000' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（黑椒味）双包1000G12' AS product_name
    UNION ALL
    SELECT '5001010014000500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（奥尔良味）0500G00' AS product_name
    UNION ALL
    SELECT '5001010060200700' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎辣条风味烤肠0700G20' AS product_name
    UNION ALL
    SELECT '5001010070000125' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎一口爆汁肠0125G00' AS product_name
    UNION ALL
    SELECT '5001010070000252' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎一口爆汁肠0252G00' AS product_name
    UNION ALL
    SELECT '5001010070210252' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎一口爆汁肠0252G21' AS product_name
    UNION ALL
    SELECT '5001010071000125' AS sku_code, '烤肠类' AS dezhou_line, '小虎一口爆汁肠（辣条味）0125G00' AS product_name
    UNION ALL
    SELECT '5001010100000500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎橄榄油鸡胸肉肠0500G00' AS product_name
    UNION ALL
    SELECT '5001010101200700' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎虎皮肉肠（原味）0700G20' AS product_name
    UNION ALL
    SELECT '5001010101213500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎虎皮肉肠（原味）3500G21' AS product_name
    UNION ALL
    SELECT '5001010102200700' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎虎皮肉肠（黑椒味）0700G20' AS product_name
    UNION ALL
    SELECT '5001010102213500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎虎皮肉肠（黑椒味）3500G21' AS product_name
    UNION ALL
    SELECT '5001010103203500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎火山石烤肠（原味）3500G20' AS product_name
    UNION ALL
    SELECT '5001010104203500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎火山石烤肠（黑椒味）3500G20' AS product_name
    UNION ALL
    SELECT '5001010120200700' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎烟熏肉肠0700G20' AS product_name
    UNION ALL
    SELECT '5001010120201500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎烟熏肉肠1500G20' AS product_name
    UNION ALL
    SELECT '5001010130000500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎辣椒炒肉烤肠0500G00' AS product_name
    UNION ALL
    SELECT '5001010180210700' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎大口肉肠（原味）0700G21' AS product_name
    UNION ALL
    SELECT '5001010180213500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎大口肉肠（原味）3500G21' AS product_name
    UNION ALL
    SELECT '5001010182213500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎大口肉肠（辣条味）3500G21' AS product_name
    UNION ALL
    SELECT '5001010190000500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎黑猪肉烤肠（原味）0500G00' AS product_name
    UNION ALL
    SELECT '5001010190210700' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎大口肉肠（黑椒味）0700G21' AS product_name
    UNION ALL
    SELECT '5001010190213500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎大口肉肠（黑椒味）3500G21' AS product_name
    UNION ALL
    SELECT '5001010191000500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎黑猪肉烤肠（黑椒味）0500G00' AS product_name
    UNION ALL
    SELECT '5001010200213500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎玉米肉肠3500G21' AS product_name
    UNION ALL
    SELECT '5001010210213500' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎台式香肠3500G21' AS product_name
    UNION ALL
    SELECT '5001020011000300' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎奥尔良V翅0300G00' AS product_name
    UNION ALL
    SELECT '5001020040000220' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎霸气手枪腿（奥尔良味）0220G00' AS product_name
    UNION ALL
    SELECT '5001020040000950' AS sku_code, '其他调理类' AS dezhou_line, '霸气手枪腿（奥尔良味）0950G00' AS product_name
    UNION ALL
    SELECT '5001020040220220' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎霸气手枪腿（奥尔良味）0220G22（DWH）' AS product_name
    UNION ALL
    SELECT '5001020041000220' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎霸气手枪腿（香辣味）0220G00' AS product_name
    UNION ALL
    SELECT '5001020041000950' AS sku_code, '其他调理类' AS dezhou_line, '霸气手枪腿（香辣味）0950G00' AS product_name
    UNION ALL
    SELECT '5001020043120180' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎霸气手枪腿（奥尔良味）0180G12' AS product_name
    UNION ALL
    SELECT '5001020050000450' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎奥尔良烤鸡0450G00' AS product_name
    UNION ALL
    SELECT '5001020051120400' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎奥尔良烤鸡0400G12' AS product_name
    UNION ALL
    SELECT '5001020080220500' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎香煎鸡排（奥尔良味）0500G22*12袋/箱' AS product_name
    UNION ALL
    SELECT '5001020082000750' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎香煎大鸡排（奥尔良味）0750G00' AS product_name
    UNION ALL
    SELECT '5001020083000750' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎香煎大鸡排（黑椒味）0750G00' AS product_name
    UNION ALL
    SELECT '5001020084000500' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎香煎鸡排（藤椒味）0500G00' AS product_name
    UNION ALL
    SELECT '5001020085000500' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎香煎鸡排（蒜香黄油味）0500G00' AS product_name
    UNION ALL
    SELECT '5001020093000500' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎香煎鱼排(黑椒味)0500G00' AS product_name
    UNION ALL
    SELECT '5001020094000500' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎香煎鱼排(蒜蓉味)0500G00' AS product_name
    UNION ALL
    SELECT '5001020110000300' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎藤椒鸡翅尖0300G00' AS product_name
    UNION ALL
    SELECT '5001020140000200' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎手打虾饼0200G00' AS product_name
    UNION ALL
    SELECT '5001020141000160' AS sku_code, '其他调理类' AS dezhou_line, '皇家小虎彩蔬虾饼0160G00' AS product_name
    UNION ALL
    SELECT '5001030030000500' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎脆皮鸡块0500G00' AS product_name
    UNION ALL
    SELECT '5001030060000500' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎黑椒鸡块0500G00' AS product_name
    UNION ALL
    SELECT '5001030060001000' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎黑椒鸡块1000G00' AS product_name
    UNION ALL
    SELECT '5001030070000500' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎脆皮鸡肉圈0500G00' AS product_name
    UNION ALL
    SELECT '5001030080000500' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎热辣香骨鸡0500G00' AS product_name
    UNION ALL
    SELECT '5001030090000200' AS sku_code, '/' AS dezhou_line, '皇家小虎鳕鱼小酥肉0200G00' AS product_name
    UNION ALL
    SELECT '5001030200000500' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎薯条0500G00' AS product_name
    UNION ALL
    SELECT '5001060011120700' AS sku_code, '鸡肉卷类' AS dezhou_line, '皇家小虎墨西哥风味鸡肉卷五连包（奥尔良风味）0700G12' AS product_name
    UNION ALL
    SELECT '5001060013221400' AS sku_code, '鸡肉卷类' AS dezhou_line, '皇家小虎老北京风味鸡肉卷1400G22' AS product_name
    UNION ALL
    SELECT '5001060014000140' AS sku_code, '鸡肉卷线' AS dezhou_line, '皇家小虎菠菜鸡肉卷（奥尔良风味）0140G00' AS product_name
    UNION ALL
    SELECT '5001060020131000' AS sku_code, '手抓饼类' AS dezhou_line, '皇家小虎手抓饼1000G13（TM）' AS product_name
    UNION ALL
    SELECT '5001060020221000' AS sku_code, '手抓饼类' AS dezhou_line, '皇家小虎手抓饼1000G22（JJL）' AS product_name
    UNION ALL
    SELECT '5001060024000800' AS sku_code, '手抓饼类' AS dezhou_line, '皇家小虎牛奶黄油手抓饼0800G00' AS product_name
    UNION ALL
    SELECT '5001060025001000' AS sku_code, '手抓饼线' AS dezhou_line, '皇家小虎梅干菜手抓饼1000G00' AS product_name
    UNION ALL
    SELECT '5001060026001000' AS sku_code, '手抓饼类' AS dezhou_line, '皇家小虎葱香味手抓饼1000G00' AS product_name
    UNION ALL
    SELECT '5001060027001000' AS sku_code, '手抓饼线' AS dezhou_line, '皇家小虎杂粮手抓饼（大黄米口味）1000G00' AS product_name
    UNION ALL
    SELECT '5001060054001100' AS sku_code, '烧麦线' AS dezhou_line, '皇家小虎糯米烧麦1100G00' AS product_name
    UNION ALL
    SELECT '5001060061000400' AS sku_code, '包点类' AS dezhou_line, '皇家小虎手工老面小笼包0400G00' AS product_name
    UNION ALL
    SELECT '5001060068000200' AS sku_code, '包点类' AS dezhou_line, '皇家小虎手工老面香辣小笼包0200G00' AS product_name
    UNION ALL
    SELECT '5001060069000200' AS sku_code, '包点类' AS dezhou_line, '皇家小虎手工老面梅干菜小笼包0200G00' AS product_name
    UNION ALL
    SELECT '5001060070000200' AS sku_code, '包点类' AS dezhou_line, '皇家小虎手工老面韭菜鸡蛋小笼包0200G00' AS product_name
    UNION ALL
    SELECT '5001060103000500' AS sku_code, '烧麦类' AS dezhou_line, '皇家小虎菌菇三鲜蒸煎饺0500G00' AS product_name
    UNION ALL
    SELECT '5001060103001000' AS sku_code, '烧麦类' AS dezhou_line, '皇家小虎菌菇三鲜蒸煎饺1000G00' AS product_name
    UNION ALL
    SELECT '5001060104000500' AS sku_code, '烧麦类' AS dezhou_line, '皇家小虎魔芋鸡胸肉蒸煎饺0500G00' AS product_name
    UNION ALL
    SELECT '5001060111000362' AS sku_code, '其他类' AS dezhou_line, '皇家小虎番茄肉酱意面0362G00' AS product_name
    UNION ALL
    SELECT '5001060141000400' AS sku_code, '烧麦类' AS dezhou_line, '皇家小虎牛奶馒头0400G00' AS product_name
    UNION ALL
    SELECT '5001060160000350' AS sku_code, '其他类' AS dezhou_line, '皇家小虎芝士焗饭（番茄肉酱味）0350G00' AS product_name
    UNION ALL
    SELECT '5001070011220600' AS sku_code, '蛋挞线' AS dezhou_line, '皇家小虎牛油蛋挞皮0600G22（YSCS）' AS product_name
    UNION ALL
    SELECT '5001070012220600' AS sku_code, '蛋挞类' AS dezhou_line, '皇家小虎商用葡式蛋挞皮0600G22' AS product_name
    UNION ALL
    SELECT '5001070013000400' AS sku_code, '蛋挞类' AS dezhou_line, '厚椰乳蛋挞液0400G00' AS product_name
    UNION ALL
    SELECT '5001070013220907' AS sku_code, '蛋挞类' AS dezhou_line, '皇家小虎商用葡式蛋挞液0907G22' AS product_name
    UNION ALL
    SELECT '5001070014000500' AS sku_code, '蛋挞类' AS dezhou_line, '皇家小虎蛋挞组合装0500G00' AS product_name
    UNION ALL
    SELECT '5001070014210448' AS sku_code, '蛋挞类' AS dezhou_line, '蛋挞组合装' AS product_name
    UNION ALL
    SELECT '5001070015220600' AS sku_code, '蛋挞类' AS dezhou_line, '皇家小虎可可蛋挞皮0600G22' AS product_name
    UNION ALL
    SELECT '5001070016000360' AS sku_code, '蛋挞类' AS dezhou_line, '皇家小虎太阳花边蛋挞皮0360G00' AS product_name
    UNION ALL
    SELECT '5001070041000120' AS sku_code, '其他类' AS dezhou_line, '皇家小虎芝士焗红薯0120G00' AS product_name
    UNION ALL
    SELECT '5001070042000120' AS sku_code, '其他类' AS dezhou_line, '皇家小虎芝士焗香芋0120G00' AS product_name
    UNION ALL
    SELECT '5001070042220150' AS sku_code, '其他类' AS dezhou_line, '皇家小虎芝士焗香芋（D）0150G22' AS product_name
    UNION ALL
    SELECT '5001070043210100' AS sku_code, '其他类' AS dezhou_line, '皇家小虎芝士焗榴莲0100G21' AS product_name
    UNION ALL
    SELECT '5001070044000120' AS sku_code, '其他类' AS dezhou_line, '皇家小虎芝士焗玉米0120G00' AS product_name
    UNION ALL
    SELECT '5001070071100200' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎意式培根披萨220G10' AS product_name
    UNION ALL
    SELECT '5001070072100200' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎黑椒牛肉披萨220G10' AS product_name
    UNION ALL
    SELECT '5001070072210220' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎黑椒牛肉披萨0220G21' AS product_name
    UNION ALL
    SELECT '5001070073100200' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎榴莲果肉披萨200G10' AS product_name
    UNION ALL
    SELECT '5001070073210220' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎榴莲果肉披萨0220G21' AS product_name
    UNION ALL
    SELECT '5001070073220220' AS sku_code, '披萨线' AS dezhou_line, '皇家小虎榴莲果肉披萨0220G22（YSCS）' AS product_name
    UNION ALL
    SELECT '5001070073230220' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎榴莲果肉披萨0220G23（YZ）' AS product_name
    UNION ALL
    SELECT '5001070074000200' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎金沙咸蛋黄嫩鸡披萨0220G00' AS product_name
    UNION ALL
    SELECT '5001070075000220' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎意式香肠披萨0220G00' AS product_name
    UNION ALL
    SELECT '5001070075210220' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎意式香肠披萨0220G21' AS product_name
    UNION ALL
    SELECT '5001070081210220' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎芝士培根披萨0220G21' AS product_name
    UNION ALL
    SELECT '5001070090000400' AS sku_code, '其他类' AS dezhou_line, '皇家小虎可颂香肠卷0400G00' AS product_name
    UNION ALL
    SELECT '5001070400000100' AS sku_code, '其他类' AS dezhou_line, '皇家小虎原味奶酪饼0100G00' AS product_name
    UNION ALL
    SELECT '5001070402000100' AS sku_code, '其他类' AS dezhou_line, '皇家小虎香芋奶酪饼0100G00' AS product_name
    UNION ALL
    SELECT '5001070501000090' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎一人食披萨（意式香肠风味）0090G00' AS product_name
    UNION ALL
    SELECT '5001070502000090' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎一人食披萨（意式培根风味）0090G00' AS product_name
    UNION ALL
    SELECT '5001070503000090' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎一人食披萨（黑椒牛肉风味）0090G00' AS product_name
    UNION ALL
    SELECT '5001070504000090' AS sku_code, '披萨类' AS dezhou_line, '皇家小虎一人食披萨（奥尔良鸡肉风味）0090G00' AS product_name
    UNION ALL
    SELECT '5001070700000252' AS sku_code, '其他类' AS dezhou_line, '皇家小虎冰烤栗0252G00' AS product_name
    UNION ALL
    SELECT '5001080014000035' AS sku_code, '其他类' AS dezhou_line, '皇家小虎新奥尔良腌料（微辣味）0035G00' AS product_name
    UNION ALL
    SELECT '5001080015000035' AS sku_code, '其他类' AS dezhou_line, '皇家小虎新奥尔良腌料（中辣味）0035G00' AS product_name
    UNION ALL
    SELECT '5001080016000035' AS sku_code, '其他类' AS dezhou_line, '皇家小虎新奥尔良腌料（蜜汁味）0035G00' AS product_name
    UNION ALL
    SELECT '5001080020000002' AS sku_code, '其他类' AS dezhou_line, '香辣蘸料/2G' AS product_name
    UNION ALL
    SELECT '5001080020000010' AS sku_code, '其他类' AS dezhou_line, '皇家小虎香辣蘸料0010G00' AS product_name
    UNION ALL
    SELECT '5001080020000100' AS sku_code, '其他类' AS dezhou_line, '皇家小虎香辣蘸料0100G00' AS product_name
    UNION ALL
    SELECT '5001080020210002' AS sku_code, '其他类' AS dezhou_line, '皇家小虎香辣蘸料0002G21' AS product_name
    UNION ALL
    SELECT '5001080021000002' AS sku_code, '其他类' AS dezhou_line, '皇家小虎孜然蘸料0002G00' AS product_name
    UNION ALL
    SELECT '5001080021000010' AS sku_code, '其他类' AS dezhou_line, '皇家小虎孜然蘸料0010G00' AS product_name
    UNION ALL
    SELECT '5001080021000100' AS sku_code, '其他类' AS dezhou_line, '皇家小虎孜然蘸料0100G00' AS product_name
    UNION ALL
    SELECT '5001080045000280' AS sku_code, '其他类' AS dezhou_line, '皇家小虎蜂蜜芥末酱0280G00' AS product_name
    UNION ALL
    SELECT '5001080051000252' AS sku_code, '其他类' AS dezhou_line, '皇家小虎焙煎芝麻沙拉汁0252G00' AS product_name
    UNION ALL
    SELECT '5001080064000280' AS sku_code, '其他类' AS dezhou_line, '皇家小虎黑胡椒酱0280G00' AS product_name
    UNION ALL
    SELECT '5001080070000280' AS sku_code, '其他类' AS dezhou_line, '皇家小虎奶香芝士酱0280G00' AS product_name
    UNION ALL
    SELECT '5001080071000280' AS sku_code, '酱料' AS dezhou_line, '皇家小虎酱香饼酱0280G00' AS product_name
    UNION ALL
    SELECT '5003030011121000' AS sku_code, '调理油炸类' AS dezhou_line, '牧老板卡兹脆鸡排1000G12' AS product_name
    UNION ALL
    SELECT '5005020088000500' AS sku_code, '其他调理类' AS dezhou_line, '饱饱博士原味香煎鸡排0500G00' AS product_name
    UNION ALL
    SELECT '5005020089000500' AS sku_code, '其他调理类' AS dezhou_line, '饱饱博士奥尔良风味香煎鸡排0500G00' AS product_name
    UNION ALL
    SELECT '5005060104000240' AS sku_code, '烧麦类' AS dezhou_line, '饱饱博士魔芋鸡胸肉蒸煎饺0240G00' AS product_name
    UNION ALL
    SELECT '5005060131000400' AS sku_code, '其他类' AS dezhou_line, '饱饱博士战斧鸡腿饱饱碗0400G00' AS product_name
    UNION ALL
    SELECT '5005060132000400' AS sku_code, '其他类' AS dezhou_line, '饱饱博士烟熏鸡排饱饱碗0400G00' AS product_name
    UNION ALL
    SELECT '5005060133000400' AS sku_code, '其他类' AS dezhou_line, '饱饱博士泡菜鸡肉饱饱碗0400G00' AS product_name
    UNION ALL
    SELECT '5005060134000400' AS sku_code, '其他类' AS dezhou_line, '饱饱博士香煎鱼排饱饱碗0400G00' AS product_name
    UNION ALL
    SELECT '5005060135000400' AS sku_code, '其他类' AS dezhou_line, '饱饱博士双拼烤肠饱饱碗0400G00' AS product_name
    UNION ALL
    SELECT '5005060136000400' AS sku_code, '其他类' AS dezhou_line, '饱饱博士培根滑蛋饱饱碗0400G00' AS product_name
    UNION ALL
    SELECT '5101010010001' AS sku_code, '/' AS dezhou_line, '小虎蛋挞热食盒大' AS product_name
    UNION ALL
    SELECT '5101010010002' AS sku_code, '/' AS dezhou_line, '小虎蛋挞热食盒小' AS product_name
    UNION ALL
    SELECT '5101010010003' AS sku_code, '/' AS dezhou_line, '小虎蛋挞盒6个装' AS product_name
    UNION ALL
    SELECT '5101010010004' AS sku_code, '/' AS dezhou_line, '小虎蛋挞盒2个装' AS product_name
    UNION ALL
    SELECT '5101010010005' AS sku_code, '/' AS dezhou_line, '小虎蛋挞盒6个装(GDMYJ)' AS product_name
    UNION ALL
    SELECT '5101010010006' AS sku_code, '/' AS dezhou_line, '小虎蛋挞盒2个装(GDMYJ)' AS product_name
    UNION ALL
    SELECT '5101010010008' AS sku_code, '/' AS dezhou_line, '零食很忙联名款热食盒6个装' AS product_name
    UNION ALL
    SELECT '5101010010009' AS sku_code, '/' AS dezhou_line, '零食很忙联名款热食盒2个装' AS product_name
    UNION ALL
    SELECT '5104010010001' AS sku_code, '/' AS dezhou_line, '饱饱饺赠品包裹卡001G00' AS product_name
    UNION ALL
    SELECT 'C06240016' AS sku_code, '其他类' AS dezhou_line, '小虎新奥尔良复合调味料蜜汁味35g' AS product_name
    UNION ALL
    SELECT 'C06240017' AS sku_code, '其他类' AS dezhou_line, '小虎新奥尔良复合调味料微辣味35g' AS product_name
    UNION ALL
    SELECT 'C06240018' AS sku_code, '其他类' AS dezhou_line, '小虎新奥尔良复合调味料中辣味35g' AS product_name
    UNION ALL
    SELECT 'FWJZY00020007' AS sku_code, '/' AS dezhou_line, '饱饱碗7天专属营养师服务兑换卡001G00' AS product_name
    UNION ALL
    SELECT 'FWJZY00040021' AS sku_code, '/' AS dezhou_line, '饱饱碗21天专属营养师服务兑换卡001G00' AS product_name
    UNION ALL
    SELECT 'HDHXHPG500g' AS sku_code, '其他调理类' AS dezhou_line, '豪德亨雪花培根500g' AS product_name
    UNION ALL
    SELECT 'HJXHDHMHZMTY500G' AS sku_code, '其他类' AS dezhou_line, '皇家小虎大黄米黑芝麻汤圆500g' AS product_name
    UNION ALL
    SELECT 'HJXH-FQJ10G' AS sku_code, '其他类' AS dezhou_line, '番茄沙司10g' AS product_name
    UNION ALL
    SELECT 'HJXH-FQSS280g' AS sku_code, '其他类' AS dezhou_line, '皇家小虎番茄沙司280g' AS product_name
    UNION ALL
    SELECT 'HJXH-HRZDNY1kg' AS sku_code, '其他类' AS dezhou_line, '皇家小虎含乳脂淡奶油1kg' AS product_name
    UNION ALL
    SELECT 'HJXH-MSLL450G' AS sku_code, '其他类' AS dezhou_line, '小虎马苏里拉奶酪450G' AS product_name
    UNION ALL
    SELECT 'HJXH-TLJ280g' AS sku_code, '其他类' AS dezhou_line, '皇家小虎甜辣酱280g' AS product_name
    UNION ALL
    SELECT 'HJXH-WDFHJWRC500g' AS sku_code, '烤肠类' AS dezhou_line, '小虎无淀粉地道肉肠黑椒味500g' AS product_name
    UNION ALL
    SELECT 'HJXH-WDFYWRC500g' AS sku_code, '烤肠类' AS dezhou_line, '小虎无淀粉地道肉肠原味500g' AS product_name
    UNION ALL
    SELECT 'HJXH-XGXRSM500g' AS sku_code, '烧麦类' AS dezhou_line, '皇家小虎香菇猪肉烧麦500g' AS product_name
    UNION ALL
    SELECT 'HJXH-XPZXYT500g' AS sku_code, '其他类' AS dezhou_line, '皇家小虎小胖子油条500g' AS product_name
    UNION ALL
    SELECT 'HJXH-XRXLB1KG' AS sku_code, '包点类' AS dezhou_line, '皇家小虎鲜肉小笼包1Kg' AS product_name
    UNION ALL
    SELECT 'MLBXJJRPG500g' AS sku_code, '其他调理类' AS dezhou_line, '牧老板香煎鸡肉培根500g' AS product_name
    UNION ALL
    SELECT 'QY-HJKC500G' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（黑椒味）0500G00' AS product_name
    UNION ALL
    SELECT 'QY-HJKC500G（ST）' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（黑椒味）0500G12' AS product_name
    UNION ALL
    SELECT 'QY-HJKC508G' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（黑椒味）0508G12' AS product_name
    UNION ALL
    SELECT 'QY-HJXHDTY500G' AS sku_code, '蛋挞类' AS dezhou_line, '小虎蛋挞液500G' AS product_name
    UNION ALL
    SELECT 'QY-HJXHJMH1000G' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎香脆鸡米花1000G' AS product_name
    UNION ALL
    SELECT 'QY-HJXHKZCJP1000G' AS sku_code, '调理油炸类' AS dezhou_line, '皇家小虎卡兹脆鸡排1000G' AS product_name
    UNION ALL
    SELECT 'QY-HJXHPSDTP480G' AS sku_code, '蛋挞类' AS dezhou_line, '小虎牛油蛋挞皮480G' AS product_name
    UNION ALL
    SELECT 'QY-HJXHPSDTP600G' AS sku_code, '蛋挞类' AS dezhou_line, '小虎牛油蛋挞皮600G' AS product_name
    UNION ALL
    SELECT 'QY-XHPG500G' AS sku_code, '其他调理类' AS dezhou_line, '小虎雪花培根500G' AS product_name
    UNION ALL
    SELECT 'QY-YMKC500G' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（玉米味）0500G00' AS product_name
    UNION ALL
    SELECT 'QY-YWKC500G' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（原味）0500G00' AS product_name
    UNION ALL
    SELECT 'QY-YWKC500G（ST）' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（原味）0500G12' AS product_name
    UNION ALL
    SELECT 'QY-YWKC508G' AS sku_code, '烤肠类' AS dezhou_line, '皇家小虎脆皮烤肠（原味）0508G12' AS product_name
    UNION ALL
    SELECT 'XH-7CNLPS180g' AS sku_code, '披萨类' AS dezhou_line, '小虎榴莲披萨盒装180g' AS product_name
    UNION ALL
    SELECT 'XH-7CNRPS180g' AS sku_code, '披萨类' AS dezhou_line, '小虎牛肉披萨盒装180g' AS product_name
    UNION ALL
    SELECT 'XHAELCG320g' AS sku_code, '其他调理类' AS dezhou_line, '小虎奥尔良翅根320g' AS product_name
    UNION ALL
    SELECT 'XHAELCZ300g' AS sku_code, '其他调理类' AS dezhou_line, '小虎奥尔良翅中300g' AS product_name
    UNION ALL
    SELECT 'XHAGSNRB100g' AS sku_code, '其他调理类' AS dezhou_line, '小虎安格斯牛肉饼100g' AS product_name
    UNION ALL
    SELECT 'XH-CYB900G' AS sku_code, '手抓饼类' AS dezhou_line, '小虎好葱油饼10片装900G' AS product_name
    UNION ALL
    SELECT 'XHDDKCHJW500g（LSHM）' AS sku_code, '烤肠类' AS dezhou_line, '小虎地道烤肠黑椒味500g（LS）' AS product_name
    UNION ALL
    SELECT 'XHDDKCYW500g（LSHM）' AS sku_code, '烤肠类' AS dezhou_line, '小虎地道烤肠原味500g（LS）' AS product_name
    UNION ALL
    SELECT 'XHDKLSDSM450G' AS sku_code, '烧麦类' AS dezhou_line, '小虎大颗粒三丁烧麦450g' AS product_name
    UNION ALL
    SELECT 'XHDSYXMZC125G' AS sku_code, '烤肠类' AS dezhou_line, '小虎德式烟熏蜜汁肠125g' AS product_name
    UNION ALL
    SELECT 'XHDTY500GXMT' AS sku_code, '蛋挞类' AS dezhou_line, '小虎蛋挞液500g（XMT）' AS product_name
    UNION ALL
    SELECT 'XHDYY500G' AS sku_code, '其他类' AS dezhou_line, '小虎大芋圆500g' AS product_name
    UNION ALL
    SELECT 'XHFQRJYM720G' AS sku_code, '其他类' AS dezhou_line, '小虎番茄肉酱意面720g' AS product_name
    UNION ALL
    SELECT 'XHHB002' AS sku_code, '/' AS dezhou_line, '2025年新年红包' AS product_name
    UNION ALL
    SELECT 'XHHHJTWJ10g' AS sku_code, '其他类' AS dezhou_line, '小虎黑胡椒调味酱10g' AS product_name
    UNION ALL
    SELECT 'XHHJNRYM720G' AS sku_code, '其他类' AS dezhou_line, '小虎黑椒牛肉意面720g' AS product_name
    UNION ALL
    SELECT 'XHHJWCPKC500gTM' AS sku_code, '烤肠类' AS dezhou_line, '小虎黑椒味脆皮烤肠500g（TM）' AS product_name
    UNION ALL
    SELECT 'XHHJWKC250G' AS sku_code, '烤肠类' AS dezhou_line, '小虎肉肠专家经典黑椒味烤肠250g' AS product_name
    UNION ALL
    SELECT 'XHHJXYP500g' AS sku_code, '其他调理类' AS dezhou_line, '小虎黑椒鲜鱼排500g' AS product_name
    UNION ALL
    SELECT 'XHHMSM400G' AS sku_code, '烧麦类' AS dezhou_line, '小虎黑米烧麦400g' AS product_name
    UNION ALL
    SELECT 'XHHRZDNY500G' AS sku_code, '其他类' AS dezhou_line, '小虎含乳脂淡奶油500g' AS product_name
    UNION ALL
    SELECT 'XHHSSRCHJW3.5kg' AS sku_code, '烤肠类' AS dezhou_line, '小虎火山石肉肠黑椒味3.5kg' AS product_name
    UNION ALL
    SELECT 'XHHSSRCHJW700g' AS sku_code, '烤肠类' AS dezhou_line, '小虎火山石肉肠黑椒味700g' AS product_name
    UNION ALL
    SELECT 'XHHSSRCYW3.5kg' AS sku_code, '烤肠类' AS dezhou_line, '小虎火山石肉肠原味3.5kg' AS product_name
    UNION ALL
    SELECT 'XHHSSRCYW700g' AS sku_code, '烤肠类' AS dezhou_line, '小虎火山石肉肠原味700g' AS product_name
    UNION ALL
    SELECT 'XH-HSZB600G' AS sku_code, '手抓饼类' AS dezhou_line, '小虎好手抓饼10片装600G' AS product_name
    UNION ALL
    SELECT 'XHHYSZB1KG' AS sku_code, '手抓饼类' AS dezhou_line, '小虎黄油手抓饼1KG' AS product_name
    UNION ALL
    SELECT 'XHHZMTY500G（LSHM）' AS sku_code, '其他类' AS dezhou_line, '小虎黑芝麻汤圆500g（LS）' AS product_name
    UNION ALL
    SELECT 'XHJCHZ130G' AS sku_code, '/' AS dezhou_line, '小虎韭菜盒子130g' AS product_name
    UNION ALL
    SELECT 'XHJCHZ780G' AS sku_code, '/' AS dezhou_line, '小虎韭菜盒子780g' AS product_name
    UNION ALL
    SELECT 'XHJCJDBPBZ420G' AS sku_code, '包点类' AS dezhou_line, '小虎韭菜鸡蛋薄皮包子420g' AS product_name
    UNION ALL
    SELECT 'XHJCJDBPBZ500G' AS sku_code, '包点类' AS dezhou_line, '小虎韭菜鸡蛋薄皮包子500g' AS product_name
    UNION ALL
    SELECT 'XHJCJDBPBZ500G（LSHM）' AS sku_code, '包点类' AS dezhou_line, '小虎韭菜鸡蛋薄皮包子500g（零食很忙）' AS product_name
    UNION ALL
    SELECT 'XHJCJDZPXB540G' AS sku_code, '包点类' AS dezhou_line, '小虎韭菜鸡蛋纸皮馅饼540g' AS product_name
    UNION ALL
    SELECT 'XHJTSCY450gX' AS sku_code, '其他类' AS dezhou_line, '小虎金汤酸菜鱼450g新' AS product_name
    UNION ALL
    SELECT 'XHKZCJP500G' AS sku_code, '调理油炸类' AS dezhou_line, '小虎卡兹脆鸡排500g' AS product_name
    UNION ALL
    SELECT 'XHLBJFWJRJ140G' AS sku_code, '鸡肉卷类' AS dezhou_line, '小虎老北京风味鸡肉卷140g' AS product_name
    UNION ALL
    SELECT 'XHLBJFWJRJ140G（LSHM）' AS sku_code, '鸡肉卷类' AS dezhou_line, '小虎老北京风味鸡肉卷140g（LS）' AS product_name
    UNION ALL
    SELECT 'XHLTFWKC400g' AS sku_code, '烤肠类' AS dezhou_line, '小虎辣条风味烤肠400g' AS product_name
    UNION ALL
    SELECT 'XHLXHTB420G' AS sku_code, '包点类' AS dezhou_line, '小虎流心核桃包420g' AS product_name
    UNION ALL
    SELECT 'XHMGCSM1.1KG' AS sku_code, '烧麦类' AS dezhou_line, '小虎梅干菜烧麦1.1kg' AS product_name
    UNION ALL
    SELECT 'XHMGCSMWHB1.1KG' AS sku_code, '烧麦类' AS dezhou_line, '小虎梅干菜烧麦无花边1.1kg' AS product_name
    UNION ALL
    SELECT 'XH-MSLLNL100g' AS sku_code, '其他类' AS dezhou_line, '小虎马苏里拉奶酪100g' AS product_name
    UNION ALL
    SELECT 'XH-MXGFWJRJARLW140G' AS sku_code, '鸡肉卷类' AS dezhou_line, '小虎墨西哥风味鸡肉卷奥尔良味140g' AS product_name
    UNION ALL
    SELECT 'XH-MXGFWJRJARLW140G（LSHM）' AS sku_code, '鸡肉卷类' AS dezhou_line, '小虎墨西哥风味鸡肉卷奥尔良味140g（LS）' AS product_name
    UNION ALL
    SELECT 'XH-MXGFWJRJXLW140G' AS sku_code, '鸡肉卷类' AS dezhou_line, '小虎墨西哥风味鸡肉卷香辣味140g' AS product_name
    UNION ALL
    SELECT 'XHMZSJB500G' AS sku_code, '包点类' AS dezhou_line, '小虎拇指生煎包500g' AS product_name
    UNION ALL
    SELECT 'XHMZSJB500G（LSHM）' AS sku_code, '包点类' AS dezhou_line, '小虎拇指生煎包500g（LS）' AS product_name
    UNION ALL
    SELECT 'XHMZSJB600G' AS sku_code, '包点类' AS dezhou_line, '小虎拇指生煎包600G' AS product_name
    UNION ALL
    SELECT 'XHNRXB520g' AS sku_code, '其他类' AS dezhou_line, '小虎牛肉馅饼520g' AS product_name
    UNION ALL
    SELECT 'XHNRXB520g（LSHM）' AS sku_code, '其他类' AS dezhou_line, '小虎牛肉馅饼520g（LS）' AS product_name
    UNION ALL
    SELECT 'XHNYDTP300G' AS sku_code, '蛋挞类' AS dezhou_line, '小虎牛油蛋挞皮300g' AS product_name
    UNION ALL
    SELECT 'XH-PGPS180g' AS sku_code, '披萨类' AS dezhou_line, '小虎培根披萨盒装180g' AS product_name
    UNION ALL
    SELECT 'XHPSDTP600G（XMT）' AS sku_code, '蛋挞类' AS dezhou_line, '小虎葡式蛋挞皮600g（XMT）' AS product_name
    UNION ALL
    SELECT 'XHQCXGBPBZ420G' AS sku_code, '包点类' AS dezhou_line, '小虎青菜香菇薄皮包子420g' AS product_name
    UNION ALL
    SELECT 'XHSGLMXLB200G' AS sku_code, '包点类' AS dezhou_line, '小虎手工老面小笼包200g' AS product_name
    UNION ALL
    SELECT 'XHSGLMXLB200G（LSHM）' AS sku_code, '包点类' AS dezhou_line, '小虎手工老面小笼包200g（LS）' AS product_name
    UNION ALL
    SELECT 'XHSGLMXLB800G' AS sku_code, '包点类' AS dezhou_line, '小虎手工老面小笼包800g' AS product_name
    UNION ALL
    SELECT 'XHSRXYP500g' AS sku_code, '其他调理类' AS dezhou_line, '小虎蒜蓉鲜鱼排500g' AS product_name
    UNION ALL
    SELECT 'XHSSSCCSCW400g' AS sku_code, '烤肠类' AS dezhou_line, '小虎膳食蔬菜肠蔬菜味400g' AS product_name
    UNION ALL
    SELECT 'XH-SZB1KG' AS sku_code, '手抓饼类' AS dezhou_line, '小虎好手抓饼10片装1kg' AS product_name
    UNION ALL
    SELECT 'XHSZB1KGTM' AS sku_code, '手抓饼类' AS dezhou_line, '小虎好手抓饼10片装1kg（TM）' AS product_name
    UNION ALL
    SELECT 'XH-SZB800G' AS sku_code, '手抓饼类' AS dezhou_line, '小虎好手抓饼10片装800G' AS product_name
    UNION ALL
    SELECT 'XH-SZB800G（LSHM）' AS sku_code, '手抓饼类' AS dezhou_line, '小虎好手抓饼10片装800G（LS）' AS product_name
    UNION ALL
    SELECT 'XHSZB800GTM' AS sku_code, '手抓饼类' AS dezhou_line, '小虎好手抓饼10片装800G（TM）' AS product_name
    UNION ALL
    SELECT 'XH-SZBZD15' AS sku_code, '/' AS dezhou_line, '手抓饼纸袋15个' AS product_name
    UNION ALL
    SELECT 'XHSZTS360G' AS sku_code, '/' AS dezhou_line, '小虎手作桃酥360g' AS product_name
    UNION ALL
    SELECT 'XHWCR200g' AS sku_code, '其他调理类' AS dezhou_line, '小虎午餐肉200g' AS product_name
    UNION ALL
    SELECT 'XH-XCJMH500g' AS sku_code, '调理油炸类' AS dezhou_line, '小虎香脆鸡米花500g' AS product_name
    UNION ALL
    SELECT 'XHXGXRSM1.1KG' AS sku_code, '烧麦类' AS dezhou_line, '皇家小虎香菇猪肉烧麦1.1kg' AS product_name
    UNION ALL
    SELECT 'XHXGXRSM400G' AS sku_code, '烧麦类' AS dezhou_line, '小虎香菇鲜肉烧麦400g' AS product_name
    UNION ALL
    SELECT 'XHXGXRSM500g（LSHM）' AS sku_code, '烧麦类' AS dezhou_line, '小虎香菇猪肉烧麦500g（LS）' AS product_name
    UNION ALL
    SELECT 'XHXGXRSMWHB1.1KG' AS sku_code, '烧麦类' AS dezhou_line, '小虎香菇鲜肉烧麦无花边1.1kg' AS product_name
    UNION ALL
    SELECT 'XHXJJPAELW500g' AS sku_code, '其他调理类' AS dezhou_line, '小虎香煎鸡排奥尔良味500g' AS product_name
    UNION ALL
    SELECT 'XHXJJPHJW500g' AS sku_code, '其他调理类' AS dezhou_line, '小虎香煎鸡排黑椒味500g' AS product_name
    UNION ALL
    SELECT 'XHXLFSBPBZ420G' AS sku_code, '包点类' AS dezhou_line, '小虎香辣粉丝薄皮包子420g' AS product_name
    UNION ALL
    SELECT 'XHXRCHJW400G' AS sku_code, '烤肠类' AS dezhou_line, '小虎鲜肉肠黑椒味400g' AS product_name
    UNION ALL
    SELECT 'XHXRCHJW400gX' AS sku_code, '烤肠类' AS dezhou_line, '小虎鲜肉肠黑椒味400g新' AS product_name
    UNION ALL
    SELECT 'XHXRCYW400g' AS sku_code, '烤肠类' AS dezhou_line, '小虎鲜肉肠原味400g' AS product_name
    UNION ALL
    SELECT 'XHXRCYW400gX' AS sku_code, '烤肠类' AS dezhou_line, '小虎鲜肉肠原味400g新' AS product_name
    UNION ALL
    SELECT 'XHXTSLJ280G' AS sku_code, '其他类' AS dezhou_line, '小虎沙拉酱280g' AS product_name
    UNION ALL
    SELECT 'XHXYY500G' AS sku_code, '其他类' AS dezhou_line, '小虎小芋圆500g' AS product_name
    UNION ALL
    SELECT 'XHYG500G' AS sku_code, '/' AS dezhou_line, '小虎椰果500g' AS product_name
    UNION ALL
    SELECT 'XHYWCPKC500gTM' AS sku_code, '烤肠类' AS dezhou_line, '小虎原味脆皮烤肠500g（TM）' AS product_name
    UNION ALL
    SELECT 'XHYWKC250G' AS sku_code, '烤肠类' AS dezhou_line, '小虎肉肠专家经典原味烤肠250g' AS product_name
    UNION ALL
    SELECT 'XHZQXLNP150g' AS sku_code, '其他调理类' AS dezhou_line, '小虎整切西冷牛排150g' AS product_name
    UNION ALL
    SELECT 'XHZQYRNP150g' AS sku_code, '其他调理类' AS dezhou_line, '小虎整切眼肉牛排150g' AS product_name
    UNION ALL
    SELECT 'XHZRDCSJ500G' AS sku_code, '烧麦类' AS dezhou_line, '小虎猪肉大葱水饺500g' AS product_name
    UNION ALL
    SELECT 'XHZRDCSJ500G（LSHM）' AS sku_code, '烧麦类' AS dezhou_line, '小虎猪肉大葱水饺500g（LS）' AS product_name
    UNION ALL
    SELECT 'XHZRMGCGKXLW450g' AS sku_code, '手抓饼类' AS dezhou_line, '小虎猪肉梅干菜锅盔（香辣味）450g' AS product_name
    UNION ALL
    SELECT 'XHZRMGCGKYW450g' AS sku_code, '手抓饼类' AS dezhou_line, '小虎猪肉梅干菜锅盔（原味）450g' AS product_name
    UNION ALL
    SELECT 'XHZRMGCGKYW450g（LSHM）' AS sku_code, '手抓饼类' AS dezhou_line, '小虎猪肉梅干菜锅盔（原味）450g（LS）' AS product_name
    UNION ALL
    SELECT 'XHZRPG200g' AS sku_code, '其他调理类' AS dezhou_line, '小虎猪肉培根200g' AS product_name
    UNION ALL
    SELECT 'XHZRPG500g' AS sku_code, '其他调理类' AS dezhou_line, '小虎猪肉培根500g' AS product_name
    UNION ALL
    SELECT 'XHZRSCXB520g' AS sku_code, '其他类' AS dezhou_line, '小虎猪肉酸菜馅饼520g' AS product_name
    UNION ALL
    SELECT 'XHZRXGSJ500G' AS sku_code, '烧麦类' AS dezhou_line, '小虎猪肉香菇水饺500g' AS product_name
    UNION ALL
    SELECT 'XHZRXGSJ500G（LSHM）' AS sku_code, '烧麦类' AS dezhou_line, '小虎猪肉香菇水饺500g （LS）' AS product_name
    UNION ALL
    SELECT 'XH-ZRXGZJJ500G' AS sku_code, '烧麦类' AS dezhou_line, '小虎猪肉香菇蒸煎饺500g' AS product_name
    UNION ALL
    SELECT 'XHZRYMZJJ1KG' AS sku_code, '烧麦类' AS dezhou_line, '小虎猪肉玉米蒸煎饺1kg' AS product_name
    UNION ALL
    SELECT 'XH-ZRYMZJJ500G' AS sku_code, '烧麦类' AS dezhou_line, '小虎猪肉玉米蒸煎饺500g' AS product_name
    UNION ALL
    SELECT 'XHZSJHS150G' AS sku_code, '其他类' AS dezhou_line, '小虎芝士焗红薯150g' AS product_name
    UNION ALL
    SELECT 'XHZSJXY150G' AS sku_code, '其他类' AS dezhou_line, '小虎芝士焗香芋150g' AS product_name
    UNION ALL
    SELECT 'XHZSNRJ120g' AS sku_code, '其他类' AS dezhou_line, '小虎芝士牛肉卷120g' AS product_name
    UNION ALL
    SELECT 'XNDL002' AS sku_code, '/' AS dezhou_line, '2025年新年对联' AS product_name
    UNION ALL
    SELECT 'ZPBGK0001001' AS sku_code, '/' AS dezhou_line, '饱饱碗赠品包裹卡001G00' AS product_name
    UNION ALL
    SELECT 'ZPCJB00010013' AS sku_code, '/' AS dezhou_line, '饱饱碗赠品餐具包0013G00' AS product_name
),
line_capacity AS (
    SELECT '烤肠类' AS dezhou_line, 243.6 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '蛋挞类' AS dezhou_line, 241.0 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '手抓饼类' AS dezhou_line, 125.0 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '披萨类' AS dezhou_line, 26.4 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '烧麦类' AS dezhou_line, 57.0 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '调理油炸类' AS dezhou_line, 29.0 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '其他调理类' AS dezhou_line, 30.0 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '鸡肉卷类' AS dezhou_line, 29.4 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '包点类' AS dezhou_line, 48.5 AS line_capacity_ton, 868.9 AS total_capacity_ton
    UNION ALL
    SELECT '其他类' AS dezhou_line, 39.0 AS line_capacity_ton, 868.9 AS total_capacity_ton
),
sku_scope AS (
    SELECT
        sku_code,
        dezhou_line,
        product_name
    FROM sku_label
    WHERE dezhou_line <> '/'
),
stock_onhand AS (
    SELECT
        DATE(stk.backups_date) AS stock_date,
        stk.cargo_owner_id,
        stk.wms_code AS warehouse_code,
        wms.wms_desc AS warehouse_name,
        stk.sku_code,
        SUM(IFNULL(stk.in_stock_quantity, 0)) AS onhand_qty
FROM ywzt_ods.scm_warehous_manage_backups stk
    JOIN params p
        ON 1 = 1
    JOIN sku_scope sl
        ON sl.sku_code = stk.sku_code
    LEFT JOIN ywzt_ods.master_wms wms
        ON stk.wms_code = wms.wms_code
WHERE stk.backups_date >= p.range_start_date
      AND stk.backups_date < p.range_end_exclusive
      AND DATE(stk.backups_date) = p.yesterday_date
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%残次%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%次品%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%废品%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%报废%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%残品%'
    GROUP BY
        DATE(stk.backups_date),
        stk.cargo_owner_id,
        stk.wms_code,
        wms.wms_desc,
        stk.sku_code
),
stock_transit AS (
    SELECT
        DATE(stk.backups_date) AS stock_date,
        stk.cargo_owner_id,
        stk.wms_code AS warehouse_code,
        stk.sku_code,
        SUM(IFNULL(stk.ship_qty, 0)) AS transit_qty
FROM ywzt_ods.scm_in_transit_backup stk
    JOIN params p
        ON 1 = 1
    JOIN sku_scope sl
        ON sl.sku_code = stk.sku_code
    LEFT JOIN ywzt_ods.master_wms wms
        ON stk.wms_code = wms.wms_code
    WHERE stk.backups_date >= p.range_start_date
      AND stk.backups_date < p.range_end_exclusive
      AND DATE(stk.backups_date) = p.yesterday_date
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%残次%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%次品%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%废品%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%报废%'
      AND IFNULL(wms.wms_desc, '') NOT LIKE '%残品%'
    GROUP BY
        DATE(stk.backups_date),
        stk.cargo_owner_id,
        stk.wms_code,
        stk.sku_code
),
yesterday_stock AS (
    SELECT
        oh.warehouse_code,
        MAX(oh.warehouse_name) AS warehouse_name,
        oh.sku_code,
        SUM(oh.onhand_qty + IFNULL(tr.transit_qty, 0)) AS yesterday_stock_qty,
        SUM((oh.onhand_qty + IFNULL(tr.transit_qty, 0)) * IFNULL(ms.net_weight, 0) / 1000) AS yesterday_stock_ton
    FROM stock_onhand oh
    LEFT JOIN stock_transit tr
        ON oh.stock_date = tr.stock_date
       AND oh.cargo_owner_id = tr.cargo_owner_id
       AND oh.warehouse_code = tr.warehouse_code
       AND oh.sku_code = tr.sku_code
    LEFT JOIN ywzt_ods.master_sku ms
        ON ms.sku_code = oh.sku_code
    GROUP BY
        oh.warehouse_code,
        oh.sku_code
),
sales_base AS (
    SELECT
        DATE(h.ship_at) AS ship_date,
        h.facility_code AS warehouse_code,
        w.wms_desc AS warehouse_name,
        i.sku_code,
        SUM(IFNULL(i.ship_qty, 0)) AS sales_qty,
        SUM(IFNULL(i.ship_qty, 0) * IFNULL(m.net_weight, 0) / 1000) AS sales_ton
FROM ywzt_ods.scm_sales_shipment_header h
    INNER JOIN ywzt_ods.scm_sales_order_header h1
        ON h1.sales_order_code = h.ref_order_code
    INNER JOIN ywzt_ods.scm_sales_shipment_detail i
        ON i.outbound_code = h.outbound_code
    JOIN params p
        ON 1 = 1
    JOIN sku_scope sl
        ON sl.sku_code = i.sku_code
    LEFT JOIN ywzt_ods.master_sku m
        ON m.sku_code = i.sku_code
    LEFT JOIN ywzt_ods.master_wms w
        ON w.wms_code = h.facility_code
    WHERE h.ship_at >= p.range_start_date
      AND h.ship_at < p.range_end_exclusive
      AND h.status IN ('700', '900', '701', '901')
      AND (
          h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK')
          OR h.ref_order_type LIKE 'SO%'
      )
      AND IFNULL(w.wms_desc, '') NOT LIKE '%残次%'
      AND IFNULL(w.wms_desc, '') NOT LIKE '%次品%'
      AND IFNULL(w.wms_desc, '') NOT LIKE '%废品%'
      AND IFNULL(w.wms_desc, '') NOT LIKE '%报废%'
      AND IFNULL(w.wms_desc, '') NOT LIKE '%残品%'
    GROUP BY
        DATE(h.ship_at),
        h.facility_code,
        w.wms_desc,
        i.sku_code
),
sales_agg AS (
    SELECT
        sb.warehouse_code,
        MAX(sb.warehouse_name) AS warehouse_name,
        sb.sku_code,
        SUM(CASE WHEN sb.ship_date = p.yesterday_date THEN sb.sales_ton ELSE 0 END) AS yesterday_sales_ton,
        SUM(CASE WHEN sb.ship_date >= DATE_SUB(p.range_end_exclusive, INTERVAL 7 DAY) THEN sb.sales_qty ELSE 0 END) / 7 AS last_7d_avg_sales_qty,
        SUM(CASE WHEN sb.ship_date >= p.range_start_date AND sb.ship_date < p.month_start_date THEN sb.sales_ton ELSE 0 END) AS last_month_sales_ton,
        SUM(CASE WHEN sb.ship_date >= p.month_start_date THEN sb.sales_ton ELSE 0 END) AS month_sales_ton
FROM sales_base sb
    JOIN params p
        ON 1 = 1
    GROUP BY
        sb.warehouse_code,
        sb.sku_code
),
purchase_base AS (
    SELECT
        DATE(d.created_date) AS purchase_date,
        h.take_delivery_wms AS warehouse_code,
        mw.wms_desc AS warehouse_name,
        d.sku_code,
        SUM(IFNULL(d.received_quantity, 0) * IFNULL(ms.net_weight, 0) / 1000) AS purchase_ton
FROM ywzt_ods.scm_po_header h
    INNER JOIN ywzt_ods.scm_po_detail d
        ON h.po_code = d.po_code
       AND h.po_type = 'CCP'
       AND d.status = 0
       AND h.status = 0
    JOIN params p
        ON 1 = 1
    JOIN sku_scope sl
        ON sl.sku_code = d.sku_code
    LEFT JOIN ywzt_ods.master_wms mw
        ON mw.wms_code = h.take_delivery_wms
    LEFT JOIN ywzt_ods.master_sku ms
        ON ms.sku_code = d.sku_code
WHERE d.created_date >= p.range_start_date
      AND d.created_date < p.range_end_exclusive
      AND IFNULL(mw.wms_desc, '') NOT LIKE '%残次%'
      AND IFNULL(mw.wms_desc, '') NOT LIKE '%次品%'
      AND IFNULL(mw.wms_desc, '') NOT LIKE '%废品%'
      AND IFNULL(mw.wms_desc, '') NOT LIKE '%报废%'
      AND IFNULL(mw.wms_desc, '') NOT LIKE '%残品%'
      AND EXISTS (
          SELECT 1
          FROM ywzt_ods.scm_warehous_header wh
          WHERE wh.po_order_code = h.po_code
            AND wh.warehous_status <> '16'
      )
    GROUP BY
        DATE(d.created_date),
        h.take_delivery_wms,
        mw.wms_desc,
        d.sku_code
),
purchase_agg AS (
    SELECT
        pb.warehouse_code,
        MAX(pb.warehouse_name) AS warehouse_name,
        pb.sku_code,
        SUM(CASE WHEN pb.purchase_date = p.yesterday_date THEN pb.purchase_ton ELSE 0 END) AS yesterday_purchase_ton,
        SUM(CASE WHEN pb.purchase_date >= p.range_start_date AND pb.purchase_date < p.month_start_date THEN pb.purchase_ton ELSE 0 END) AS last_month_purchase_ton,
        SUM(CASE WHEN pb.purchase_date >= p.month_start_date THEN pb.purchase_ton ELSE 0 END) AS month_purchase_ton
FROM purchase_base pb
    JOIN params p
        ON 1 = 1
    GROUP BY
        pb.warehouse_code,
        pb.sku_code
),
all_keys AS (
    SELECT warehouse_code, sku_code FROM yesterday_stock
    UNION
    SELECT warehouse_code, sku_code FROM sales_agg
    UNION
    SELECT warehouse_code, sku_code FROM purchase_agg
)
SELECT
    ak.sku_code AS `产品编码`,
    sl.dezhou_line AS `德州产线`,
    COALESCE(ms.sku_name, sl.product_name) AS `商品名称`,
    lc.line_capacity_ton AS `产线产能吨`,
    lc.total_capacity_ton AS `总产能吨`,
    ak.warehouse_code AS `仓库编码`,
    COALESCE(ys.warehouse_name, sa.warehouse_name, pa.warehouse_name) AS `仓库名称`,
    IFNULL(ys.yesterday_stock_qty, 0) AS `昨日库存数量`,
    IFNULL(ys.yesterday_stock_ton, 0) AS `昨日库存吨数`,
    IFNULL(sa.yesterday_sales_ton, 0) AS `昨日销售吨数`,
    IFNULL(pa.yesterday_purchase_ton, 0) AS `昨日采购吨数`,
    IFNULL(sa.last_7d_avg_sales_qty, 0) AS `近7天平均销售数量`,
    IFNULL(sa.last_month_sales_ton, 0) AS `上月销售吨数`,
    IFNULL(pa.last_month_purchase_ton, 0) AS `上月采购吨数`,
    IFNULL(sa.month_sales_ton, 0) AS `本月销售吨数`,
    IFNULL(pa.month_purchase_ton, 0) AS `本月采购吨数`
FROM all_keys ak
INNER JOIN sku_scope sl
    ON sl.sku_code = ak.sku_code
LEFT JOIN ywzt_ods.master_sku ms
    ON ms.sku_code = ak.sku_code
LEFT JOIN line_capacity lc
    ON lc.dezhou_line = sl.dezhou_line
LEFT JOIN yesterday_stock ys
    ON ys.warehouse_code = ak.warehouse_code
   AND ys.sku_code = ak.sku_code
LEFT JOIN sales_agg sa
    ON sa.warehouse_code = ak.warehouse_code
   AND sa.sku_code = ak.sku_code
LEFT JOIN purchase_agg pa
    ON pa.warehouse_code = ak.warehouse_code
   AND pa.sku_code = ak.sku_code
