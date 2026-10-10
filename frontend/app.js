(function () {
  "use strict";

  const THEME_KEY = "royal-little-tiger-query-theme";
  const UI_HISTORY_KEY_PREFIX = "royal-little-tiger-query-history-v1:";
  const UI_HISTORY_VERSION = 1;
  const MAX_PERSISTED_SESSIONS = 40;
  const MAX_PERSISTED_EVENTS_PER_SESSION = 80;
  const MAX_PERSISTED_HISTORY_BYTES = 1500000;
  const RELOAD_INTERRUPTED_MESSAGE = "上次请求在刷新时中断，请重新开始";
  const TIGER_ASSET_BASE = "/static/assets/royal-tiger";
  const TIGER_AVATAR_SRC = `${TIGER_ASSET_BASE}/tiger-avatar.jpg`;
  // The server allows one model turn to run for up to five minutes. The
  // browser waits slightly longer; retries use the same idempotency key.
  const CHAT_TIMEOUT_MS = 930000;
  const REQUEST_POLL_DELAY_MS = 1200;
  const REQUEST_POLL_LIMIT = 600;
  const BUSY_REQUEST_RETRY_LIMIT = REQUEST_POLL_LIMIT;
  const HEALTH_TIMEOUT_MS = 8000;
  const TRANSIENT_REQUEST_RETRY_LIMIT = 3;
  const TRANSIENT_REQUEST_RETRY_BASE_MS = 700;
  const TRANSIENT_HTTP_STATUSES = new Set([502, 503, 504]);
  const REPLAYABLE_ERROR_CODES = new Set([
    "network_error",
    "request_timeout",
    "request_in_progress",
    "conversation_busy",
    "reload_interrupted"
  ]);
  const MAX_OPTION_LENGTH = 300;
  const MAX_PROMPT_LENGTH = 500;
  const MAX_QUESTION_LENGTH = 4000;
  const CONVERSION_QUESTION = "将 MySQL 查询转换为 Doris";
  const MAX_CONVERSION_BYTES = 200000;
  let conversionReadNonce = 0;
  let conversionReading = false;
  const MAX_BUSINESS_RESULT_LENGTH = 8000;
  const MAX_BUSINESS_TABLE_ROWS = 500;
  const MAX_BUSINESS_TABLE_COLUMNS = 32;
  const resultPageStates = new Map();
  const drilldownJobs = new WeakSet();
  const batchDownloads = new Set();
  const reportJobs = new Set();
  let reportDialogSources = [];
  let reportDialogBusy = false;
  const detailExporter = globalThis.DetailExport?.create({
    auth: () => ({conversation:state.backendConversationId, csrf:state.csrfToken}),
    changed: () => renderMessages()
  });
  const tableExporter = globalThis.TableExport?.create({
    loadPage: async (resultId, page) => normalizeBusinessTable(
      (await fetchJsonWithRetry(`/v1/results/${encodeURIComponent(resultId)}/pages/${page}`, {}, HEALTH_TIMEOUT_MS, true)).business_table,
      "completed"
    ),
    notify: (message) => showToast(message),
  });
  const DETAIL_COLUMNS = "cargo_owner,warehouse_code,outbound_code,sku_code,ship_at,quantity";

  function normalizeDetailSelection(raw) {
    return raw && /^db_[A-Za-z0-9_-]{32}$/.test(raw.result_id || "") && Number.isSafeInteger(raw.batch_index) && raw.batch_index > 0
      ? {result_id: raw.result_id, batch_index: raw.batch_index} : null;
  }

  function normalizeDetailMetadata(raw) {
    if (!raw || !/^db_[A-Za-z0-9_-]{32}$/.test(raw.result_id || "")
      || !["next_batch", "loaded_rows", "total_rows", "expires_at"].every(key => Number.isSafeInteger(raw[key]))
      || raw.next_batch < 1 || raw.loaded_rows < 0 || raw.total_rows < 1 || raw.loaded_rows > raw.total_rows || raw.expires_at < 1
      || typeof raw.complete !== "boolean" || raw.complete !== (raw.loaded_rows === raw.total_rows)
      || (raw.loaded_rows === 0 ? raw.next_batch !== 1 : raw.next_batch < 2)) return null;
    return {result_id: raw.result_id, next_batch: raw.next_batch, loaded_rows: raw.loaded_rows,
      total_rows: raw.total_rows, complete: raw.complete, expires_at: raw.expires_at,
      ...(/^ex_[a-f0-9]{32}$/.test(raw.export_id || "") ? {export_id:raw.export_id} : {})};
  }
  const RANKING_COLUMNS = "rank,sku_code,sku_name,quantity,outbound_count";
  const DRILLDOWN_COLUMNS = "sku_code,cargo_owner,warehouse_code,warehouse_name,customer_code,outbound_code,ship_at,quantity";

  function normalizePagination(raw, rowCount) {
    if (!raw || typeof raw !== "object" || !/^rp_[A-Za-z0-9_-]{32}$/.test(raw.result_id || "")) return null;
    const keys = ["page", "page_count", "total_rows", "start_row", "end_row", "expires_at"];
    if (keys.some((key) => !Number.isSafeInteger(raw[key]) || raw[key] < 1)) return null;
    if (raw.reconciled !== true || raw.page > raw.page_count || raw.total_rows > 100000 || raw.page_count > raw.total_rows || raw.end_row > raw.total_rows || raw.start_row > raw.end_row || raw.end_row - raw.start_row + 1 !== rowCount) return null;
    return { result_id: raw.result_id, ...Object.fromEntries(keys.map((key) => [key, raw[key]])), reconciled: true };
  }
  const MAX_BUSINESS_TABLE_TEXT_LENGTH = 240;
  const MAX_BUSINESS_TABLE_BYTES = 240000;
  const BUSINESS_RESULT_UNSAFE_PATTERN = /```|<\s*(?:script|iframe|object|embed)\b|\b(?:select|insert|update|delete|drop|truncate|alter|create|merge|with|from|join|where|union|having|group\s+by|order\s+by|information_schema|sql|bindings?|credentials?|password|secret|access[_ -]?token|api[_ -]?key|webhook)\b|(?:查询语句|SQL语句|数据库凭据|审计内容|执行日志|原始数据行|原始结果行|字段列表|表结构|尚待业务审批|尚待审批|待业务审批|尚未审批发布|口径未发布|未审批发布)|\b[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*\b|`/i;
  const BUSINESS_TABLE_UNSAFE_PATTERN = /```|<\s*(?:script|iframe|object|embed)\b|\b(?:select|insert|update|delete|drop|truncate|alter|create|merge|with|from|join|where|union|having|group\s+by|order\s+by|information_schema|sql|bindings?|credentials?|password|secret|access[_ -]?token|api[_ -]?key|webhook)\b|(?:查询语句|SQL语句|数据库凭据|审计内容|执行日志|原始数据行|原始结果行|字段列表|表结构)|\b[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*\b|`/i;
  const BUSINESS_TABLE_COLUMN_DEFS = Object.freeze({
    order_created_at: Object.freeze({ label: "订单创建时间", kind: "text", align: "left" }),
    request_quantity: Object.freeze({ label: "原始请求数量", kind: "number", align: "right", nullable: true }),
    quantity_unit_reference: Object.freeze({ label: "源数量单位代码", kind: "text", align: "left", nullable: true }),
    sales_unit_reference: Object.freeze({ label: "源销售单位代码", kind: "text", align: "left", nullable: true }),
    order_status: Object.freeze({ label: "订单状态", kind: "text", align: "left", nullable: true }),
    order_created: Object.freeze({ label: "订单创建时间", kind: "text", align: "left" }),
    order_detail_state: Object.freeze({ label: "明细状态", kind: "text", align: "left" }),
    order_ship_quantity: Object.freeze({ label: "订单实发原始数量", kind: "number", align: "right", nullable: true, signed: true }),
    returned_quantity: Object.freeze({ label: "退货原始数量", kind: "number", align: "right", signed: true }),
    net_sales_quantity: Object.freeze({ label: "净销售原始数量", kind: "number", align: "right", nullable: true, signed: true }),
    return_document_count: Object.freeze({ label: "关联退货单数", kind: "number", align: "right", unit: "单" }),
    sales_detail_count: Object.freeze({ label: "销售明细行数", kind: "number", align: "right" }),
    return_detail_count: Object.freeze({ label: "退货明细行数", kind: "number", align: "right" }),
    quantity_state: Object.freeze({ label: "数量状态", kind: "text", align: "left" }),
    order_date: Object.freeze({ label: "创建日期", kind: "text", align: "left" }),
    province: Object.freeze({ label: "省区", kind: "text", align: "left", nullable: true }),
    manager_key: Object.freeze({ label: "经理标识", kind: "text", align: "left", nullable: true }),
    manager_name: Object.freeze({ label: "城市经理", kind: "text", align: "left", nullable: true }),
    manager_state: Object.freeze({ label: "经理资料状态", kind: "text", align: "left" }),
    missing_manager_order_count: Object.freeze({ label: "经理缺失订单数", kind: "number", align: "right", unit: "单" }),
    referenced_order_code: Object.freeze({ label: "引用的销售订单", kind: "text", align: "left", nullable: true }),
    linked_sales_order_code: Object.freeze({ label: "已匹配销售订单", kind: "text", align: "left", nullable: true }),
    source_order_code: Object.freeze({ label: "头部来源单号", kind: "text", align: "left", nullable: true }),
    detail_source_order_code: Object.freeze({ label: "订单来源编码", kind: "text", align: "left", nullable: true }),
    source_platform_code: Object.freeze({ label: "来源平台", kind: "text", align: "left", nullable: true }),
    referenced_order_type: Object.freeze({ label: "出库关联类型", kind: "text", align: "left", nullable: true }),
    order_type: Object.freeze({ label: "订单类型", kind: "text", align: "left", nullable: true }),
    link_ship_at: Object.freeze({ label: "出库时间", kind: "text", align: "left", nullable: true }),
    relationship_state: Object.freeze({ label: "关联状态", kind: "text", align: "left" }),
    sales_order_code: Object.freeze({ label: "订单号", kind: "text", align: "left" }),
    customer_code: Object.freeze({ label: "客户编码", kind: "text", align: "left", nullable: true }),
    customer_name: Object.freeze({ label: "客户名称", kind: "text", align: "left", nullable: true }),
    created_time: Object.freeze({ label: "创建时间", kind: "text", align: "left" }),
    shipment_name: Object.freeze({ label: "出库记录名称", kind: "text", align: "left", nullable: true }),
    order_name: Object.freeze({ label: "订单记录名称", kind: "text", align: "left", nullable: true }),
    master_name_variants: Object.freeze({ label: "主数据名称数", kind: "number", align: "right" }),
    missing_name_rows: Object.freeze({ label: "空名称记录数", kind: "number", align: "right" }),
    missing_name_sources: Object.freeze({ label: "缺失来源数", kind: "number", align: "right" }),
    name_variants: Object.freeze({ label: "不同名称数", kind: "number", align: "right" }),
    name_status: Object.freeze({ label: "名称状态", kind: "text", align: "left" }),
    customer_channel: Object.freeze({ label: "客户渠道", kind: "text", align: "left" }),
    channel_code: Object.freeze({ label: "渠道编码", kind: "text", align: "left" }),
    channel_name: Object.freeze({ label: "渠道细分", kind: "text", align: "left" }),
    quantity_share: Object.freeze({ label: "实发数量占比", kind: "text", align: "right" }),
    box_spec: Object.freeze({ label: "箱规", kind: "number", align: "right", unit: "EA/箱", nullable: true }),
    box_quantity: Object.freeze({ label: "实发箱数", kind: "number", align: "right", unit: "箱", nullable: true }),
    box_state: Object.freeze({ label: "换算状态", kind: "text", align: "left" }),
    inbound_code: Object.freeze({ label: "入库单", kind: "text", align: "left" }),
    production_day: Object.freeze({ label: "生产日期", kind: "text", align: "left" }),
    source_unit: Object.freeze({ label: "来源单位", kind: "text", align: "left" }),
    receipt_count: Object.freeze({ label: "入库单数", kind: "number", align: "right", unit: "单" }),
    detail_count: Object.freeze({ label: "明细行数", kind: "number", align: "right", unit: "行" }),
    planned_quantity: Object.freeze({ label: "计划入库数量", kind: "number", align: "right", unit: "按来源单位" }),
    empty_production_detail_count: Object.freeze({ label: "生产日期为空明细", kind: "number", align: "right", unit: "行" }),
    zero_outbound_count: Object.freeze({ label: "零数量出库单数", kind: "number", align: "right", unit: "单" }),
    sales_order_count: Object.freeze({ label: "销售订单数", kind: "number", align: "right", unit: "单" }),
    outbound_tons: Object.freeze({ label: "出库吨数", kind: "number", align: "right", unit: "吨", signed: true }),
    missing_net_weight_quantity: Object.freeze({ label: "缺少净重数量", kind: "number", align: "right", unit: "EA", signed: true }),
    detail_id: Object.freeze({ label: "明细行标识", kind: "text", align: "left" }),
    item_code: Object.freeze({ label: "行号", kind: "text", align: "left", nullable: true }),
    batch_number: Object.freeze({ label: "批次", kind: "text", align: "left", nullable: true }),
    received_quantity: Object.freeze({ label: "实收入库数量", kind: "number", align: "right" }),
    quantity_unit: Object.freeze({ label: "来源单位编码", kind: "text", align: "left" }),
    actual_arrival_date: Object.freeze({ label: "实际入库时间", kind: "text", align: "left" }),
    ship_date: Object.freeze({ label: "出库日期", kind: "text", align: "left" }),
    shipment_status: Object.freeze({ label: "出库状态", kind: "text", align: "left" }),
    outbound_count: Object.freeze({ label: "出库单数", kind: "number", align: "right", unit: "单" }),
    outbound_code: Object.freeze({ label: "出库单号", kind: "text", align: "left" }),
    ship_at: Object.freeze({ label: "出库时间", kind: "text", align: "left" }),
    warehouse: Object.freeze({ label: "仓库", kind: "text", align: "left" }),
    warehouse_code: Object.freeze({ label: "仓库编码", kind: "text", align: "left" }),
    warehouse_name: Object.freeze({ label: "仓库名称", kind: "text", align: "left", nullable: true }),
    warehouse_type: Object.freeze({ label: "仓库类型", kind: "text", align: "left" }),
    warehouse_node: Object.freeze({ label: "仓库节点", kind: "text", align: "left" }),
    quantity: Object.freeze({ label: "实际销售出库", kind: "number", align: "right", unit: "EA" }),
    fulfillment_2c: Object.freeze({ label: "2C 实发数量", kind: "number", align: "right", unit: "EA", signed: true }),
    fulfillment_2b: Object.freeze({ label: "2B 实发数量", kind: "number", align: "right", unit: "EA", signed: true }),
    total_quantity: Object.freeze({ label: "总实发数量", kind: "number", align: "right", unit: "EA", signed: true }),
    rank: Object.freeze({ label: "排名", kind: "number", align: "center" }),
    sku_name: Object.freeze({ label: "商品", kind: "text", align: "left" }),
    sku_code: Object.freeze({ label: "SKU", kind: "text", align: "left" }),
    order_count: Object.freeze({ label: "订单数", kind: "number", align: "right", unit: "单" }),
    ref_order_type: Object.freeze({ label: "出库类型", kind: "text", align: "left" }),
    cargo_owner: Object.freeze({ label: "货主", kind: "text", align: "left" }),
    fulfillment_type: Object.freeze({ label: "履约类型", kind: "text", align: "left" }),
    sku_count: Object.freeze({ label: "SKU数", kind: "number", align: "right", unit: "个" }),
    period_1_quantity: Object.freeze({ label: "期间一出库数量", kind: "number", align: "right", unit: "EA", nullable: true }),
    period_2_quantity: Object.freeze({ label: "期间二出库数量", kind: "number", align: "right", unit: "EA", nullable: true }),
    period_1_daily_average: Object.freeze({ label: "期间一日均量", kind: "number", align: "right", unit: "EA/日", nullable: true }),
    period_2_daily_average: Object.freeze({ label: "期间二日均量", kind: "number", align: "right", unit: "EA/日", nullable: true }),
    period_1_record_state: Object.freeze({ label: "期间一记录状态", kind: "text", align: "left" }),
    period_2_record_state: Object.freeze({ label: "期间二记录状态", kind: "text", align: "left" }),
    daily_average_difference: Object.freeze({ label: "日均量差额", kind: "number", align: "right", unit: "EA/日", nullable: true, signed: true }),
    daily_average_change_rate: Object.freeze({ label: "日均变化率", kind: "text", align: "right" }),
    warehouse_count: Object.freeze({ label: "仓库数", kind: "number", align: "right", unit: "个" }),
    warehouse_share: Object.freeze({ label: "数量占比（%）", kind: "number", align: "right" }),
    in_stock: Object.freeze({ label: "在库数量", kind: "number", align: "right", unit: "EA", signed: true }),
    stock_lock: Object.freeze({ label: "锁定数量", kind: "number", align: "right", unit: "EA", signed: true }),
    freeze: Object.freeze({ label: "冻结数量", kind: "number", align: "right", unit: "EA", signed: true }),
    available: Object.freeze({ label: "可用量", kind: "number", align: "right", unit: "EA", signed: true }),
    average_7d_outbound: Object.freeze({ label: "近7天平均出库量", kind: "number", align: "right", unit: "EA/日", signed: true }),
    available_days: Object.freeze({ label: "库存可用天数", kind: "number", align: "right", unit: "天", nullable: true }),
    metric: Object.freeze({ label: "指标", kind: "text", align: "left" }),
    value: Object.freeze({ label: "数值", kind: "number", align: "right", nullable: true }),
    unit: Object.freeze({ label: "单位", kind: "text", align: "center" }),
    production_date: Object.freeze({ label: "生产日期", kind: "text", align: "left" }),
     current_stock: Object.freeze({ label: "当前库存", kind: "number", align: "right", unit: "EA" }),
     belonging_center: Object.freeze({ label: "归属中心仓", kind: "text", align: "left" }),
     available_stock: Object.freeze({ label: "实际库存", kind: "number", align: "right", unit: "EA" }),
    snapshot_stock: Object.freeze({ label: "历史快照库存", kind: "number", align: "right", unit: "EA", nullable: true }),
    snapshot_date: Object.freeze({ label: "快照日期", kind: "text", align: "left" }),
    inbound_quantity: Object.freeze({ label: "入库数量", kind: "number", align: "right", unit: "EA" }),
    inbound_tons: Object.freeze({ label: "入库吨数", kind: "number", align: "right", unit: "吨" }),
    transfer_occupation: Object.freeze({ label: "调拨占用", kind: "number", align: "right", unit: "EA", signed: true }),
    sales_occupation: Object.freeze({ label: "销售占用", kind: "number", align: "right", unit: "EA", signed: true }),
    occupation_total: Object.freeze({ label: "占用合计", kind: "number", align: "right", unit: "EA", signed: true }),
    associated_order_count: Object.freeze({ label: "关联单号数", kind: "number", align: "right", unit: "个" }),
    month: Object.freeze({ label: "月份", kind: "text", align: "left" }),
    redemption_quantity: Object.freeze({ label: "兑换数量", kind: "number", align: "right", unit: "台" }),
    customer_count: Object.freeze({ label: "客户数", kind: "number", align: "right", unit: "个" }),
    air_fryer_quantity: Object.freeze({ label: "空气炸锅兑换数量", kind: "number", align: "right", unit: "件", signed: true }),
    daily_sales: Object.freeze({ label: "区间日均销量", kind: "number", align: "right", unit: "EA/日" }),
    inventory: Object.freeze({ label: "可用成品库存 + 在途", kind: "number", align: "right", unit: "EA", signed: true, nullable: true }),
    available_inventory: Object.freeze({ label: "可用库存", kind: "number", align: "right", unit: "EA", signed: true }),
    in_transit_inventory: Object.freeze({ label: "在途库存", kind: "number", align: "right", unit: "EA", signed: true }),
    finished_goods_inventory: Object.freeze({ label: "成品库存", kind: "number", align: "right", unit: "EA", signed: true }),
    in_transit_share: Object.freeze({ label: "在途占比", kind: "number", align: "right", unit: "%", nullable: true }),
    inventory_share: Object.freeze({ label: "库存占比", kind: "number", align: "right", unit: "%", nullable: true }),
    daily_average: Object.freeze({ label: "实际销售出库日均", kind: "number", align: "right", unit: "EA/日", nullable: true }),
    cumulative_quantity: Object.freeze({ label: "累计实发数量", kind: "number", align: "right", unit: "EA" }),
    moving_average: Object.freeze({ label: "移动平均", kind: "number", align: "right", unit: "EA/日", nullable: true }),
    turnover_days: Object.freeze({ label: "库存周转", kind: "number", align: "right", unit: "天", nullable: true }),
    turnover_state: Object.freeze({ label: "数据状态", kind: "text", align: "left" }),
    turnover_text: Object.freeze({ label: "周转天数", kind: "text", align: "right" })
  });
  const BUSINESS_TABLE_LABELS = new Set([
    "日期", "累计实发数量", "移动平均",
    "原始请求数量", "源数量单位代码", "源销售单位代码", "订单状态",
    "销售订单", "订单创建时间", "明细状态", "订单实发原始数量", "退货原始数量", "净销售原始数量", "关联退货单数", "销售明细行数", "退货明细行数", "数量状态",
    "创建日期", "省区", "经理标识", "城市经理", "经理资料状态", "经理缺失订单数",
    "引用的销售订单", "已匹配销售订单", "头部来源单号", "订单来源编码", "来源平台", "出库关联类型", "订单类型", "关联状态",
    "订单号", "客户编码", "客户名称", "创建时间", "创建月份",
    "出库记录名称", "订单记录名称", "主数据名称数", "空名称记录数", "缺失来源数", "不同名称数", "名称状态",
    "客户渠道", "箱规", "实发箱数", "换算状态",
    "入库单", "入库单数", "明细行数", "来源单位", "明细行标识", "行号", "批次", "实收入库数量", "来源单位编码", "实际入库时间",
    "出库日期", "出库状态", "出库单数", "零数量出库单数", "销售订单数", "出库吨数", "缺少净重数量", "出库单号", "出库时间", "实发数量",
    "仓库编码", "仓库名称", "仓库类型", "仓库节点",
     "仓库", "分仓", "实际销售出库", "出库类型", "货主", "履约类型", "排名", "商品", "SKU", "销售出库", "订单数",
    "渠道编码", "渠道细分", "2C/2B", "SKU数", "实发数量占比", "期间一出库数量", "期间二出库数量",
    "期间一日均量", "期间二日均量", "期间一记录状态", "期间二记录状态", "日均量差额", "日均变化率", "仓库数", "仓库数量", "数量占比（%）", "货主编码", "实际调拨出库",
    "指标", "数值", "单位", "生产日期", "生产日期为空明细", "计划入库数量", "当前库存", "实际可用", "历史快照库存", "快照日期", "入库数量", "入库吨数", "调拨占用", "销售占用", "占用合计", "关联单号数", "在库数量", "锁定数量", "冻结数量", "可用量", "近7天平均出库量", "库存可用天数",
    "月份", "兑换数量", "兑换订单数", "客户数", "空气炸锅兑换数量", "经营中心", "实际成品库存", "区间日均销量", "周转天数", "实际库存", "2C 实发数量", "2B 实发数量", "总实发数量", "可用库存", "在途库存", "成品库存", "在途占比", "库存占比",
     "可用成品库存 + 在途", "可用成品库存＋在途", "近7天日均销量", "近14天日均销量", "实际销售出库日均", "日均实发数量", "归属中心仓", "库存周转", "数据状态"
  ]);
  const BUSINESS_TABLE_UNITS = new Set(["EA", "EA/日", "EA/箱", "箱", "天", "个", "单", "吨", "台", "行", "按来源单位", "个月", "个完整自然日"]);
  const MODEL_RESULT_UNITS = new Set([...BUSINESS_TABLE_UNITS,
    "件", "元", "万元", "%", "千克", "克", "小时", "分钟"]);
  const RATIO_NUMERATOR_UNITS = ["EA", "件", "箱", "吨", "台", "千克", "克", "元", "万元", "单", "个"];
  const RATIO_DENOMINATOR_UNITS = ["单", "个", "日", "天", "周", "月", "小时", "分钟", "EA", "件", "箱", "吨", "千克", "克"];
  for (const numerator of RATIO_NUMERATOR_UNITS) {
    for (const denominator of RATIO_DENOMINATOR_UNITS) {
      if (numerator !== denominator) MODEL_RESULT_UNITS.add(`${numerator}/${denominator}`);
    }
  }
  const BUSINESS_TABLE_SUMMARY_LABELS = new Set(["订单数", "城市经理缺失订单数", "明细行数", "合计", "实际销售出库", "出库单数", "出库吨数", "SKU数", "SKU数（分组合计）", "仓库数", "有效 SKU", "统计范围销售出库", "统计天数", "当前库存", "当前实际可用", "当前实际库存", "兑换总量", "兑换订单数", "有数据月份", "入榜实发数量", "入榜出库单数", "实发数量", "来源记录", "生产日期为空明细", "空气炸锅兑换量", "缺少净重数量", "计划入库数量", "销售订单数", "入库单数", "入库单数（分组合计）", "实收入库数量"]);
  const CLARIFICATION_TOKEN_PATTERN = /^ct_[A-Za-z0-9_-]{20,160}$/;
  const REPORT_ARTIFACT_ID_PATTERN = /^art_[A-Za-z0-9_-]{24,80}$/;
  const REPORT_FILE_ID_PATTERN = /^rpf_[A-Za-z0-9_-]{24,80}$/;
  const REPORT_ID_PATTERN = /^rpt_[a-f0-9]{32}$/;
  const REPORT_FILE_FORMATS = new Set(["html", "pdf", "word", "excel", "csv", "png"]);
  const FEEDBACK_ENDPOINT = "/v1/metric-feedback";
  const QUERY_FEEDBACK_ENDPOINT = "/v1/query-feedback";
  const LEARNING_SUMMARY_ENDPOINT = "/v1/learning/summary";
  const REPORT_KIND = "center_warehouse_turnover_7d";
  const TURNOVER_CONFLICT_PROMPTS = Object.freeze({
    tc_variant: "请选择本次讨论的历史方案；选择不代表口径已发布。",
    tc_stock: "库存采用哪个时点？",
    tc_sales: "销售数量采用哪个业务口径？",
    tc_returns: "销售数量是否扣减退货？",
    tc_scope: "货主、仓库范围采用哪一种？",
    tc_window: "销售日均量采用哪个时间区间？"
  });
  const TURNOVER_CONFLICT_LABELS = Object.freeze({
    tc_a: "A：当前实时可用库存 / 指定区间日均销售出库",
    tc_b: "B：月初库存与本月累计出库（公式待确认）",
    tc_realtime: "当前实时可用库存（不含在途）",
    tc_month_start: "本月月初历史库存快照",
    tc_actual: "实际完成的销售出库数量",
    tc_allocated: "分配数量（单位关系未确认）",
    tc_keep_returns: "不扣减退货，按出库量统计",
    tc_deduct_returns: "扣减退货（退货口径待确认）",
    tc_all: "全部货主、全部仓库；其余未说明范围均为全部",
    tc_specified: "指定范围（未明确的范围暂不执行）",
    tc_7days: "最近7个完整自然日",
    tc_mtd: "本自然月1日至提问当天00:00",
    tc_custom: "指定日期区间"
  });
  const DATE_RANGE_OPTIONS = Object.freeze({ tc_window: "tc_custom" });
  const EXECUTION_STATUS_MESSAGES = new Set([
    "本次未形成可确认的实时数据结果，请重新提交",
    "本次取数未完成，请稍后重试",
    "报表口径尚未审批发布，暂未生成图片",
    "数据质量校验未通过，暂未生成图片",
    "报表查询未完成，暂未生成图片",
    "数据图片生成失败，请稍后重试",
    "报表口径尚未完成配置",
    "查询成功，但当前条件下没有符合条件的数据。",
    "查询未执行，原因是后端安全校验拒绝了该 SQL。",
    "查询执行失败，请稍后重试。",
    "查询服务暂时不可用，请稍后重试。",
    "查询结果被截断，当前结果不完整，未作为完整结果展示。",
    "Doris 查询已完成，但 Agent 未能解析返回结果，请稍后重试。"
  ]);
  const QUERY_RESULT_STATES = new Set([
    "succeeded", "empty_result", "query_rejected", "query_failed",
    "query_unavailable", "truncated_result", "invalid_result"
  ]);
  const PRESENTATION_ERROR_MESSAGE = "查询已完成，但部分结果未通过页面格式校验。已保留可展示的内容，请刷新页面，无需重新取数。";
  const CLARIFICATION_ERROR_MESSAGE = "口径选项无法显示，本次尚未查询数据。请刷新页面恢复选择。";
  EXECUTION_STATUS_MESSAGES.add(CLARIFICATION_ERROR_MESSAGE);
  // Only these reviewed reason combinations can cross the presentation boundary.
  for (const business of ["", "所选出库业务尚未接入已核验的查询"]) {
    for (const measure of ["", "所选数量的单位口径尚未确认，不能用实发数量代替", "所选单数统计尚未接入此查询组合，不能用数量代替"]) {
      for (const time of ["", "所选业务时间尚未接入此查询组合，不能用实际出库时间代替"]) {
        const reasons = [business, measure, time].filter(Boolean);
        if (reasons.length) EXECUTION_STATUS_MESSAGES.add(`已保留本次选择。${reasons.join("；")}。本次未执行查询，无需重复选择。`);
      }
    }
  }
  const TURNOVER_CONFLICT_REASONS = ["库存时点与方案不一致", "销售区间与方案不一致", "分配数量单位待确认", "退货扣减口径待确认", "指定范围尚未形成完整查询条件"];
  for (let mask = 0; mask < 2 ** TURNOVER_CONFLICT_REASONS.length; mask++) {
    const reasons = ["历史方案尚未发布，不能替代已确认的成品库存加在途口径", ...TURNOVER_CONFLICT_REASONS.filter((_, index) => mask & (1 << index))];
    EXECUTION_STATUS_MESSAGES.add(`已保留本次选择。${reasons.join("；")}。本次未执行查询，无需重复选择。`);
  }
  const LEARNING_ROUTE_LABELS = Object.freeze({
    inbound: "入库",
    sales_outbound: "销售出库",
    inventory_turnover: "库存周转",
    inventory: "库存",
    dms_points_redemption: "DMS 积分兑换",
    procurement: "采购与在途",
    other: "其他"
  });
  const CLARIFICATION_RECOVERY_CODES = new Set([
    "clarification_pending",
    "clarification_invalid"
  ]);
  // Legacy wording is a fallback only. Model-owned business choices use the
  // same bounded, plain-text contract as the backend, not an enumerated menu.
  const SAFE_COPY = new Set([
    ...Object.values(TURNOVER_CONFLICT_PROMPTS),
    "请选择出库业务。", "请选择统计项；实发、分配、请求数量与单数不能互相代替。", "昨天按哪个业务时间统计？",
    "名称检查对应哪种出库口径？", "实际销售出库，核对名称", "其他出库口径，暂不执行",
    "选择库存口径",
    "选择时间窗口",
    "请选择要查询的业务指标。",
    "请确认本次业务口径。",
    "请补充本次取数的业务口径。",
    "您要查实际完成出库量，还是订单请求量或特定渠道替换口径？",
    "请确认要查当前可用库存，还是指定日期的历史库存快照；如查历史，请提供准确日期。",
    "请确认周转分子用当前可用库存，分母用实际完成销售出库量，是否采用该口径？",
    "请选择周转分子；分母固定为所选区间内实际完成销售出库成品的日均数量。",
    "请确认本月库存周转口径：分子使用当前可用库存，分母使用本月实际完成销售出库量的日均值吗？",
    "请选择库存周转口径",
    "请选择销售时间窗口",
    "请选择库存口径。",
    "请选择结果粒度",
    "请选择查询范围",
    "若选择历史库存快照，请选择或提供准确的快照日期；不能自动使用最近日期。",
    "可用库存",
    "毛库存",
    "实时",
    "指定日期",
    "今天",
    "昨天",
    "实际完成",
    "订单请求",
    "实际完成出库量",
    "订单请求量",
    "特定渠道替换口径",
    "历史库存快照和实际完成出库量",
    "当前可用库存和订单请求量",
    "区间末日成品库存快照",
    "当前实时成品库存",
    "本月完整自然日",
    "上个自然月",
    "最近7个完整自然日",
    "本月1日至今天00:00",
    "指定开始和结束时间",
    "最近30个完整日",
    "当前可用库存",
    "历史库存快照",
    "实际销售出库量",
    "本月库存周转",
    "本月库存周转天数",
    "区间末成品库存周转",
    "当前实时成品库存周转",
    "当前实时可用库存",
    "当前可用库存",
    "指定日期历史库存快照",
    "是，按当前可用库存和实际完成出库量",
    "否，使用其他口径，需提交新的业务口径",
    "否，提交新的业务口径",
    "提交新的业务口径",
    "历史报表兼容口径（采购申请量全额计在途）",
    "剩余未收口径（仅统计尚未收货数量）",
    "请选择库存口径。范围固定为8个经营中心及当前有效分仓，动销固定为最近7个完整自然日的有效完成出库，结果将生成汇总图片。",
    "可用库存 + 在途库存｜产成品｜8个经营中心及当前有效分仓",
    "实时实际库存｜产成品｜8个经营中心及当前有效分仓",
    "总计",
    "按仓库",
    "按SKU",
    "按仓库和SKU",
    "全部货主",
    "全部仓库",
    "全部SKU",
    "指定准确日期",
    "是",
    "否"
  ]);
  const SAFE_LABEL_BY_ID = Object.freeze({
    ...TURNOVER_CONFLICT_LABELS,
    outbound_sales: "销售出库",
    outbound_transfer: "调拨出库（按完成状态和实际发出数量）",
    outbound_adjustment: "调整出库（暂未接入）",
    outbound_other: "其他出库（暂未接入）",
    outbound_all: "所有出库（销售与调拨）",
    outbound_actual_qty: "实发数量",
    outbound_allocated_qty: "分配数量（单位待确认）",
    outbound_requested_qty: "请求数量（单位待确认）",
    outbound_document_count: "出库单数",
    outbound_order_count: "销售订单数（暂未接入）",
    outbound_shipped_at: "实际出库时间",
    outbound_order_created_at: "订单创建时间（暂未接入）",
    outbound_reviewed_at: "审核时间（暂未接入）",
    sku_name_actual: "实际销售出库，核对名称",
    sku_name_defer: "其他出库口径，暂不执行",
    outbound_link_latest: "查询范围内最新出库的一张单",
    outbound_link_all: "查询范围内全部出库单",
    actual: "实际完成",
    actual_completed_outbound: "实际完成出库量",
    order: "订单请求",
    order_request: "订单请求量",
    order_requested_quantity: "订单请求量",
    channel_replacement: "特定渠道替换口径",
    available: "可用库存",
    gross: "毛库存",
    realtime: "实时",
    date: "指定日期",
    today: "今天",
    yesterday: "昨天",
    current: "当前可用库存",
    historical: "历史库存快照",
    current_available: "当前可用库存",
    historical_snapshot: "指定日期历史库存快照",
    current_available_realtime: "当前实时可用库存",
    today_historical_snapshot: "指定日期历史库存快照",
    sales_outbound_actual: "实际销售出库量",
    inventory_available_realtime: "当前可用库存",
    inventory_snapshot_quantity: "历史库存快照",
    inventory_turnover_mtd_realtime: "本月库存周转",
    inventory_turnover_finished_goods_snapshot: "区间末成品库存周转",
    inventory_turnover_finished_goods_realtime: "当前实时成品库存周转",
    current_available_actual_outbound: "当前实时成品库存",
    historical_snapshot_actual_outbound: "区间末日成品库存快照",
    current_available_order_request: "当前可用库存和订单请求量",
    period_end_finished_goods_snapshot: "区间末日成品库存快照",
    current_realtime_finished_goods: "当前实时成品库存",
    mtd_complete_days: "本月1日至今天00:00",
    mtd_to_today_00: "本月1日至今天00:00",
    custom_period: "指定开始和结束时间",
    explicit_period: "指定开始和结束时间",
    last_30_complete_days: "最近30个完整日",
    previous_natural_month: "上个自然月",
    last_7_complete_days: "最近7个完整自然日",
    other_basis: "否，使用其他口径，需提交新的业务口径",
    provide_business_definition: "提交新的业务口径",
    available_plus_transit_finished_goods: "可用库存 + 在途库存｜产成品｜8个经营中心及当前有效分仓",
    actual_finished_goods: "可用成品库存 + 在途｜8个经营中心及当前有效分仓",
    record_deduction_all: "记录库存扣减后（全库存状态；8个经营中心）",
    total: "总计",
    warehouse: "按仓库",
    sku: "按SKU",
    warehouse_sku: "按仓库和SKU",
    all_owner: "全部货主",
    all_warehouse: "全部仓库",
    all_sku: "全部SKU",
    production_date: "生产日期库存",
    in_transit: "在途库存",
    legacy_full_requested_transit: "历史报表兼容口径（采购申请量全额计在途）",
    remaining_unreceived_transit: "剩余未收口径（仅统计尚未收货数量）",
    pickup_availability: "线下可提货库存",
    provide_date: "指定准确日期",
    yes: "是",
    no: "否"
  });
  const SAFE_SEMANTIC_IDS = new Set([
    ...Object.keys(TURNOVER_CONFLICT_PROMPTS), ...Object.keys(TURNOVER_CONFLICT_LABELS),
    "outbound_business", "outbound_measure", "outbound_time_axis",
    "outbound_sales", "outbound_transfer", "outbound_adjustment", "outbound_other", "outbound_all",
    "outbound_actual_qty", "outbound_allocated_qty", "outbound_requested_qty", "outbound_document_count", "outbound_order_count",
    "outbound_shipped_at", "outbound_order_created_at", "outbound_reviewed_at",
    "sku_name_outbound_basis", "sku_name_actual", "sku_name_defer",
    "outbound_link_scope", "outbound_link_latest", "outbound_link_all",
    "metric", "basis", "inventory_basis", "inventory_quantity_basis", "window", "time_window",
    "snapshot", "shipment_basis", "shipment_quantity_basis", "transit_quantity_basis", "outbound_basis", "turnover_basis", "sales_window",
    "turnover_component_basis", "turnover_inventory_point", "q_specification", "available", "gross", "realtime",
    "date", "today", "yesterday", "actual", "actual_completed_outbound", "order", "order_request", "order_requested_quantity", "channel_replacement",
    "current", "historical", "current_available", "historical_snapshot", "yes", "no", "provide_business_definition",
    "sales_outbound_actual", "inventory_available_realtime", "inventory_snapshot_quantity",
    "inventory_turnover_mtd_realtime", "inventory_turnover_finished_goods_snapshot", "inventory_turnover_finished_goods_realtime",
    "current_available_realtime", "today_historical_snapshot",
    "current_available_actual_outbound", "historical_snapshot_actual_outbound",
    "current_available_order_request", "period_end_finished_goods_snapshot", "current_realtime_finished_goods",
    "mtd_complete_days", "mtd_to_today_00", "previous_natural_month", "last_7_complete_days", "custom_period", "explicit_period", "last_30_complete_days",
    "other_basis", "output_grain", "scope",
    "total", "warehouse", "sku", "warehouse_sku", "all_owner", "all_warehouse", "all_sku",
    "production_date", "in_transit", "legacy_full_requested_transit", "remaining_unreceived_transit", "pickup_availability", "snapshot_date", "provide_date",
    "turnover_inventory_profile", "available_plus_transit_finished_goods", "actual_finished_goods", "record_deduction_all"
  ]);

  const elements = {
    body: document.body,
    sidebar: document.getElementById("sidebar"),
    sidebarToggle: document.getElementById("sidebarToggle"),
    mobileBackdrop: document.getElementById("mobileBackdrop"),
    newChatButton: document.getElementById("newChatButton"),
    reportEntryButton: document.getElementById("reportEntryButton"),
    definitionButton: document.getElementById("definitionButton"),
    learningButton: document.getElementById("learningButton"),
    learningCount: document.getElementById("learningCount"),
    clearHistoryButton: document.getElementById("clearHistoryButton"),
    conversationList: document.getElementById("conversationList"),
    conversationEmpty: document.getElementById("conversationEmpty"),
    breadcrumbTitle: document.getElementById("breadcrumbTitle"),
    connectionStatus: document.getElementById("connectionStatus"),
    contextStatus: document.getElementById("contextStatus"),
    sidebarStatusDot: document.getElementById("sidebarStatusDot"),
    sidebarStatusText: document.getElementById("sidebarStatusText"),
    themeToggle: document.getElementById("themeToggle"),
    inspector: document.getElementById("inspector"),
    inspectorToggle: document.getElementById("inspectorToggle"),
    inspectorClose: document.getElementById("inspectorClose"),
    inspectorEmpty: document.getElementById("inspectorEmpty"),
    inspectorContent: document.getElementById("inspectorContent"),
    chatScroll: document.getElementById("chatScroll"),
    welcomeState: document.getElementById("welcomeState"),
    messageList: document.getElementById("messageList"),
    rulePicker: document.getElementById("rulePicker"),
    ruleCatalog: document.getElementById("ruleCatalog"),
    ruleCatalogVersion: document.getElementById("ruleCatalogVersion"),
    ruleForm: document.getElementById("ruleForm"),
    ruleFormTitle: document.getElementById("ruleFormTitle"),
    ruleFormDescription: document.getElementById("ruleFormDescription"),
    ruleFormClose: document.getElementById("ruleFormClose"),
    ruleFields: document.getElementById("ruleFields"),
    ruleSubmit: document.getElementById("ruleSubmit"),
    composerForm: document.getElementById("composerForm"),
    questionInput: document.getElementById("questionInput"),
    stopButton: document.getElementById("stopButton"),
    sendButton: document.getElementById("sendButton"),
    conversionUpload: document.getElementById("conversionUpload"),
    conversionFile: document.getElementById("conversionFile"),
    conversionInputStatus: document.getElementById("conversionInputStatus"),
    proposalUpload: document.getElementById("proposalUpload"),
    proposalFile: document.getElementById("proposalFile"),
    proposalInputStatus: document.getElementById("proposalInputStatus"),
    charCount: document.getElementById("charCount"),
    toast: document.getElementById("toast"),
    traceStatusText: document.getElementById("traceStatusText"),
    traceContext: document.getElementById("traceContext"),
    traceCompression: document.getElementById("traceCompression"),
    traceElapsed: document.getElementById("traceElapsed"),
    traceOutcome: document.getElementById("traceOutcome"),
    traceProcess: document.getElementById("traceProcess"),
    definitionDialog: document.getElementById("definitionDialog"),
    definitionForm: document.getElementById("definitionForm"),
    definitionClose: document.getElementById("definitionClose"),
    definitionCancel: document.getElementById("definitionCancel"),
    definitionSubmit: document.getElementById("definitionSubmit"),
    reportDialog: document.getElementById("reportDialog"),
    reportForm: document.getElementById("reportForm"),
    reportClose: document.getElementById("reportClose"),
    reportCancel: document.getElementById("reportCancel"),
    reportSourceSelect: document.getElementById("reportSourceSelect"),
    reportSourceEmpty: document.getElementById("reportSourceEmpty"),
    reportTitleInput: document.getElementById("reportTitleInput"),
    reportAudienceInput: document.getElementById("reportAudienceInput"),
    reportInstructionsInput: document.getElementById("reportInstructionsInput"),
    reportSubmit: document.getElementById("reportSubmit"),
    reportDialogStatus: document.getElementById("reportDialogStatus"),
    reportDialogDownloads: document.getElementById("reportDialogDownloads"),
    reportQueryButton: document.getElementById("reportQueryButton"),
    metricNameInput: document.getElementById("metricNameInput"),
    metricDefinitionInput: document.getElementById("metricDefinitionInput"),
    metricScopeInput: document.getElementById("metricScopeInput"),
    metricUnitInput: document.getElementById("metricUnitInput"),
    metricExampleInput: document.getElementById("metricExampleInput"),
    metricNotesInput: document.getElementById("metricNotesInput"),
    queryFeedbackDialog: document.getElementById("queryFeedbackDialog"),
    queryFeedbackForm: document.getElementById("queryFeedbackForm"),
    queryFeedbackClose: document.getElementById("queryFeedbackClose"),
    queryFeedbackCancel: document.getElementById("queryFeedbackCancel"),
    queryFeedbackSubmit: document.getElementById("queryFeedbackSubmit"),
    queryFeedbackNote: document.getElementById("queryFeedbackNote"),
    learningDialog: document.getElementById("learningDialog"),
    learningClose: document.getElementById("learningClose"),
    learningSummary: document.getElementById("learningSummary")
  };

  const state = {
    sessions: [],
    activeId: null,
    nextSessionNumber: 1,
    backendConversationId: "",
    historyStorageKey: "",
    loading: false,
    recoveringClarification: false,
    sessionReady: false,
    identityMode: "",
    csrfToken: "",
    requestNonce: 0,
    panelReturnFocus: null,
    contextState: null,
    inspectorSnapshot: null,
    feedbackSubmitting: false,
    queryFeedbackSubmitting: false,
    feedbackTarget: null,
    learningSummary: null
  };
  const ruleState = { catalog: null, selected: null };

  const STAGE_LABELS = Object.freeze({
    received: "已接收请求",
    accepted: "已接收请求",
    context: "已处理用户上下文",
    route: "正在识别业务口径",
    routing: "正在识别业务口径",
    clarify: "正在确认业务口径",
    authorization: "正在校验访问权限",
    authorize: "正在校验访问权限",
    preparing: "正在准备只读执行",
    compile: "正在准备只读执行",
    execute: "正在执行 Doris",
    executing: "正在执行 Doris",
    query: "正在执行 Doris",
    compaction: "已请求上下文压缩",
    finalize: "正在整理执行状态",
    finalizing: "正在整理执行状态",
    complete: "执行阶段已完成",
    completed: "执行阶段已完成",
    blocked: "本次未返回数据",
    failed: "执行未完成"
  });

  const OUTCOME_LABELS = Object.freeze({
    completed: "执行完成",
    success: "执行完成",
    clarification_required: "等待一次性选择",
    clarify: "等待一次性选择",
    blocked: "本次未返回数据",
    failed: "执行未完成",
    error: "执行未完成",
    running: "正在执行",
    pending: "等待执行"
  });

  const PROCESS_STATE_ALIASES = Object.freeze({
    completed: "completed",
    complete: "completed",
    succeeded: "completed",
    success: "completed",
    started: "completed",
    reused: "completed",
    ready: "completed",
    running: "running",
    executing: "running",
    active: "running",
    pending: "pending",
    queued: "pending",
    waiting: "pending",
    requested: "requested",
    not_due: "not_due",
    notdue: "not_due",
    unchanged: "not_due",
    failed: "failed",
    error: "failed",
    warning: "warning",
    blocked: "blocked"
  });

  // Keep the visible lifecycle short. Context reuse, routing, SQL preparation,
  // and compaction remain internal evidence; users only need the four business
  // phases that explain where a request is.
  const PROCESS_STAGE_GROUPS = Object.freeze({
    received: "planning", accepted: "planning", context: "planning",
    route: "planning", routing: "planning", authorization: "planning",
    authorize: "planning", preparing: "planning", compile: "planning",
    clarify: "clarify", query: "query", execute: "query", executing: "query",
    compaction: "validation", finalize: "validation", finalizing: "validation",
    complete: "completed", completed: "completed", failed: "failed", blocked: "blocked"
  });

  const CONTEXT_LABELS = Object.freeze({
    inherited: "已继承",
    fresh: "已建立",
    unavailable: "待绑定"
  });

  const COMPRESSION_LABELS = Object.freeze({
    compressed: "已自动压缩",
    requested: "已请求压缩",
    enabled: "已启用",
    unchanged: "本轮未触发",
    unavailable: "待启用"
  });

  function randomId(prefix = "view") {
    const generated = window.crypto?.randomUUID?.();
    return generated
      ? `${prefix}_${generated}`
      : `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  }

  function createSession() {
    const number = state.nextSessionNumber;
    state.nextSessionNumber += 1;
    return {
      id: randomId(),
      title: `取数会话 ${number}`,
      createdAt: Date.now(),
      events: [],
      contextState: null,
      pendingClarification: null,
      reportKind: null,
      reportProfileId: null
    };
  }

  function activeSession() {
    if (!state.sessions.length) {
      const session = createSession();
      state.sessions.push(session);
      state.activeId = session.id;
    }
    return state.sessions.find((item) => item.id === state.activeId) || state.sessions[0];
  }

  function cloneClarification(rawClarification) {
    if (!rawClarification || rawClarification.required === false) return null;
    const source = rawClarification.required === true
      ? rawClarification
      : {
          required: true,
          token: rawClarification.token,
          draft_id: rawClarification.draft_id,
          questions: rawClarification.questions
        };
    const normalized = normalizeClarification(source);
    if (!normalized || normalized.invalid || normalized.required !== true) return null;
    return {
      required: true,
      token: normalized.token,
      draft_id: normalized.draft_id,
      questions: normalized.questions.map((question) => ({
        id: question.id,
        prompt: question.prompt,
        multiple: question.multiple,
        ...(question.dateRangeOption ? { date_range_option: question.dateRangeOption } : {}),
        options: question.options.map((option) => ({
          id: option.id,
          label: option.label,
          available: option.available !== false,
        }))
      }))
    };
  }

  function pendingClarificationEvent(session) {
    if (!session || !Array.isArray(session.events)) return null;
    for (let index = session.events.length - 1; index >= 0; index -= 1) {
      const event = session.events[index];
      if (event?.kind === "clarification" && event.clarification?.required === true) return event;
    }
    return null;
  }

  function pendingClarificationForSession(session) {
    if (session?.pendingClarification?.required === true) return session.pendingClarification;
    return pendingClarificationEvent(session)?.clarification || null;
  }

  function latestQuestionForSession(session) {
    if (!session || !Array.isArray(session.events)) return "";
    for (let index = session.events.length - 1; index >= 0; index -= 1) {
      const event = session.events[index];
      const question = displayQuestionText(event?.question || event?.sourceQuestion);
      if (question) return question;
    }
    return "";
  }

  function hasAnyPendingClarification() {
    return state.sessions.some((session) => Boolean(pendingClarificationForSession(session)));
  }

  function removeEvent(session, event) {
    if (!session || !event || !Array.isArray(session.events)) return false;
    const index = session.events.indexOf(event);
    if (index < 0) return false;
    session.events.splice(index, 1);
    return true;
  }

  function requestEventIndex(session, requestKey) {
    if (!session || !requestKey || !Array.isArray(session.events)) return -1;
    return session.events.findIndex((event) => event?.requestKey === requestKey);
  }

  function ownsRequest(session, requestNonce, runningEvent) {
    return Boolean(
      session &&
      runningEvent &&
      state.requestNonce === requestNonce &&
      state.sessions.includes(session) &&
      session.events.includes(runningEvent)
    );
  }

  function replaceRequestEvent(session, requestKey, event) {
    const index = requestEventIndex(session, requestKey);
    if (index < 0) return false;
    session.events[index] = event;
    return true;
  }

  function upsertPendingClarification(
    session,
    rawClarification,
    context = null,
    updateGlobalContext = true,
    sourceQuestion = ""
  ) {
    const clarification = cloneClarification(rawClarification);
    if (!session || !clarification) return null;
    const existingEvent = pendingClarificationEvent(session);
    const resolvedSourceQuestion = displayQuestionText(sourceQuestion) ||
      displayQuestionText(existingEvent?.sourceQuestion) ||
      latestQuestionForSession(session);
    const event = {
      kind: "clarification",
      clarification,
      sourceQuestion: resolvedSourceQuestion,
      selectedAnswers: clarification.draft_id && existingEvent?.clarification?.draft_id === clarification.draft_id
        ? normalizeStoredAnswers(existingEvent.selectedAnswers, clarification) : null,
      context: context || session.contextState || state.contextState || normalizeContext(null)
    };
    let existingIndex = -1;
    for (let index = session.events.length - 1; index >= 0; index -= 1) {
      if (session.events[index]?.kind === "clarification") {
        existingIndex = index;
        break;
      }
    }
    if (existingIndex >= 0) session.events[existingIndex] = event;
    else session.events.push(event);
    session.pendingClarification = clarification;
    if (context) {
      session.contextState = context;
      if (updateGlobalContext) state.contextState = context;
    }
    return clarification;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  let richTextParser = null;
  function renderRichText(value) {
    const text = String(value ?? "");
    if (typeof window.markdownit !== "function") {
      return `<p>${escapeHtml(text).replace(/\r?\n/g, "<br>")}</p>`;
    }
    if (!richTextParser) {
      richTextParser = window.markdownit({ html: false, linkify: false, breaks: true, typographer: false });
      richTextParser.validateLink = (url) => /^https?:\/\//i.test(url);
      // Only authenticated report artifacts may load images; answer text is not an image source.
      richTextParser.disable("image");
      richTextParser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
        tokens[index].attrSet("target", "_blank");
        tokens[index].attrSet("rel", "noopener noreferrer");
        return renderer.renderToken(tokens, index, options);
      };
      for (const type of ["fence", "code_block"]) {
        richTextParser.renderer.rules[type] = (tokens, index) =>
          `<pre class="rich-code-block" tabindex="0" aria-label="代码块"><code>${escapeHtml(tokens[index].content)}</code></pre>`;
      }
      richTextParser.renderer.rules.table_open = () => '<div class="rich-table-scroll" tabindex="0" role="region" aria-label="回答表格"><table>';
      richTextParser.renderer.rules.table_close = () => '</table></div>';
    }
    return richTextParser.render(text);
  }

  function boundedText(value, maxLength) {
    return String(value ?? "")
      .replace(/[\u0000-\u001f\u007f]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, maxLength);
  }

  function displayQuestionText(value) {
    return String(value ?? "")
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, " ")
      .replace(/\r\n?/g, "\n")
      .trim()
      .slice(0, MAX_QUESTION_LENGTH);
  }

  function safeChoiceText(value, limit) {
    if (typeof value !== "string") return "";
    const text = value.replace(/\s+/g, " ").trim();
    if (!text || text === "该选项" || text.length > limit || BUSINESS_TABLE_UNSAFE_PATTERN.test(text) ||
        /[<>`=;]|\b(?:rows?|columns?|records?|result|token|table|schema|field|binding|ywzt_ods|dms_ods)\b|\b(?:private|internal)[-_ ]?(?:row|data|result|table|detail)\b|(?:表名|字段名|物理表|原始行|结果明细|执行明细)|\bsk-[a-z0-9_-]{16,}\b/i.test(text)) return "";
    return text;
  }

  function safePrompt(value) {
    return safeChoiceText(value, MAX_PROMPT_LENGTH);
  }

  function safeOptionLabel(value, optionId = "") {
    // Never replace valid server wording by a legacy label for the same ID.
    if (value == null || value === "") return SAFE_LABEL_BY_ID[optionId] || "";
    return safeChoiceText(value, MAX_OPTION_LENGTH);
  }

  function safeId(value) {
    if (typeof value !== "string") return "";
    const candidate = value.trim();
    // Opaque server-bound IDs must round-trip unchanged through selection.
    return /^[A-Za-z0-9_.:@-]{1,120}$/.test(candidate) &&
      !/\b(?:sql|select|insert|update|delete|drop|truncate|alter|create|from|join|where|union|rows?|columns?|bindings?|records?|result|secret|password|credential|token|table)\b/i.test(candidate)
      ? candidate : "";
  }

  function formatTime(timestamp) {
    return new Date(timestamp || Date.now()).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function normalizeKey(value) {
    return boundedText(value, 80).toLowerCase().replace(/[\s-]+/g, "_");
  }

  function stageKey(value) {
    const key = normalizeKey(value);
    if (STAGE_LABELS[key]) return key;
    if (key === "context" || key.includes("context")) return "context";
    if (key === "compaction" || key.includes("compact")) return "compaction";
    if (key.includes("auth") || key.includes("permission")) return "authorization";
    if (key.includes("route") || key.includes("metric")) return "routing";
    if (key.includes("prepar") || key.includes("compile")) return "preparing";
    if (key.includes("execut") || key.includes("query")) return "executing";
    if (key.includes("final") || key.includes("result")) return "finalizing";
    if (key.includes("clarif") || key.includes("select")) return "clarify";
    if (key.includes("fail") || key.includes("error")) return "failed";
    if (key.includes("block")) return "blocked";
    if (key.includes("complete") || key.includes("success")) return "completed";
    return "received";
  }

  function outcomeKey(value) {
    const key = normalizeKey(value);
    if (OUTCOME_LABELS[key]) return key;
    if (key.includes("clarif") || key.includes("choice")) return "clarification_required";
    if (key.includes("block")) return "blocked";
    if (key.includes("fail") || key.includes("error")) return "failed";
    if (key.includes("run") || key.includes("pend")) return "running";
    if (key.includes("success") || key.includes("complete")) return "completed";
    return "pending";
  }

  function safeProcessState(value, fallback = "completed") {
    return PROCESS_STATE_ALIASES[normalizeKey(value)] || fallback;
  }

  function compactStageKey(value) {
    const canonical = normalizeKey(value);
    if (["planning", "validation"].includes(canonical)) return canonical;
    const key = stageKey(value);
    return PROCESS_STAGE_GROUPS[key] || "planning";
  }

  function stageLabel(step) {
    const id = compactStageKey(step?.id || step?.state);
    const status = safeProcessState(step?.state);
    if (status === "blocked") return "执行已停止";
    if (status === "failed") return "执行未完成";
    if (status === "warning") return "结果已整理，待复核";
    if (id === "planning") {
      if (status === "running") return "正在解析业务条件";
      if (status === "pending") return "等待解析业务条件";
      return "业务条件已明确";
    }
    if (id === "clarify") return status === "pending" ? "等待口径选择" : "业务口径已确认";
    if (id === "query") {
      if (status === "completed") return "Doris 查询已返回";
      if (status === "pending") return "等待 Doris 只读查询";
      return "正在执行 Doris";
    }
    if (id === "validation") {
      if (status === "not_due") return "结果已校验整理";
      if (status === "pending") return "等待结果校验";
      if (status === "running") return "正在校验结果";
      if (status === "completed") return "结果已校验整理";
    }
    if (id === "completed") return "执行完成";
    return STAGE_LABELS[id] || "执行阶段";
  }

  function normalizeContext(rawContext) {
    const raw = rawContext && typeof rawContext === "object" ? rawContext : {};
    const scope = normalizeKey(raw.scope);
    const freshPerRequest = raw.query_context_mode === "fresh_per_request";
    const inherited = !freshPerRequest && (raw.inherited === true || raw.has_context === true ||
      (raw.has_context !== false && (scope === "principal" || scope === "conversation")));
    const compressionCount = Number(raw.compaction_count);
    const compressed = raw.compressed === true || (Number.isFinite(compressionCount) && compressionCount > 0);
    const pending = raw.compaction_pending === true || normalizeKey(raw.status) === "compacting";
    const enabled = raw.compression_enabled !== false && raw.status !== "disabled";
    return {
      inherited,
      compressed,
      pending,
      enabled,
      contextLabel: inherited ? CONTEXT_LABELS.inherited : CONTEXT_LABELS.fresh,
      compressionLabel: pending
        ? COMPRESSION_LABELS.requested
        : compressed
          ? COMPRESSION_LABELS.compressed
          : enabled
            ? COMPRESSION_LABELS.unchanged
            : COMPRESSION_LABELS.unavailable
    };
  }

  function normalizeClarification(rawClarification) {
    if (!rawClarification || rawClarification.required !== true || !Array.isArray(rawClarification.questions)) {
      return null;
    }
    if (rawClarification.questions.length === 0 || rawClarification.questions.length > 12) {
      return { required: false, invalid: true };
    }
    const questionIds = new Set();
    const questions = rawClarification.questions.map((question, questionIndex) => {
      if (!question || typeof question !== "object" || !Array.isArray(question.options) || question.options.length === 0 || question.options.length > 20) {
        return null;
      }
      const questionId = safeId(question?.id);
      if (!questionId || questionIds.has(questionId)) return null;
      questionIds.add(questionId);
      const prompt = safePrompt(question.prompt);
      if (!prompt) return null;
      const multiple = question?.multiple === true || normalizeKey(question?.selection) === "multiple";
      const dateRangeOption = DATE_RANGE_OPTIONS[questionId] || "";
      if (question.date_range_option && question.date_range_option !== dateRangeOption) return null;
      if (dateRangeOption && (multiple || !question.options.some((option) => option?.id === dateRangeOption))) return null;
      const optionIds = new Set();
      const options = question.options.map((option, optionIndex) => {
        const optionId = safeId(option?.id);
        if (!optionId || optionIds.has(optionId)) return null;
        optionIds.add(optionId);
        const label = safeOptionLabel(option?.label, optionId);
        if (!label) return null;
        return { id: optionId, label, available: option.available !== false };
      });
      if (options.some((option) => !option)) return null;
      return {
        id: questionId,
        prompt,
        multiple,
        dateRangeOption,
        options
      };
    });
    if (questions.some((question) => !question)) return { required: false, invalid: true };
    const tokenCandidate = boundedText(rawClarification.token, 512);
    const token = CLARIFICATION_TOKEN_PATTERN.test(tokenCandidate) ? tokenCandidate : "";
    const draftId = /^cd_[a-f0-9]{64}$/.test(rawClarification.draft_id || "") ? rawClarification.draft_id : "";
    return { required: true, questions, token, draft_id: draftId };
  }

  function clarificationFromPayload(payload) {
    if (payload?.clarification && typeof payload.clarification === "object") {
      return payload.clarification;
    }
    if (Array.isArray(payload?.questions)) {
      return {
        required: payload.required !== false,
        token: payload.token,
        questions: payload.questions
      };
    }
    return null;
  }

  function normalizeProcess(rawProcess, outcome) {
    const raw = rawProcess && typeof rawProcess === "object" ? rawProcess : {};
    const rawSteps = Array.isArray(rawProcess) ? rawProcess : Array.isArray(raw.steps) ? raw.steps : [];
    const seen = new Set();
    const steps = [];
    if (outcome === "clarification_required") {
      return [{id: "planning", state: "completed"}, {id: "clarify", state: "pending"}];
    }
    rawSteps.slice(0, 12).forEach((step) => {
      // Context compaction is not evidence of business result validation.
      if (normalizeKey(step?.id) === "compaction") return;
      const id = compactStageKey(step?.id || step?.state);
      if (seen.has(id)) return;
      seen.add(id);
      steps.push({ id, state: safeProcessState(step?.state) });
    });
    if (!steps.length) {
      const fallback = outcome === "clarification_required"
        ? [["planning", "completed"], ["clarify", "pending"]]
        : outcome === "failed" || outcome === "blocked"
          ? [["planning", "completed"], [outcome, outcome]]
          : outcome === "running"
             ? [["planning", "running"]]
            : [["planning", "completed"], ["query", "completed"], ["validation", "completed"], ["completed", "completed"]];
      fallback.forEach(([id, state]) => steps.push({ id, state }));
    }
    return steps;
  }

  function normalizeBusinessResult(rawResult, outcome) {
    if (outcome !== "completed" || !rawResult || typeof rawResult !== "object") return null;
    if (rawResult.format !== "plain_text" || typeof rawResult.text !== "string") return null;
    const text = rawResult.text.replace(/\r\n?/g, "\n").trim();
    if (!text || text.length > MAX_BUSINESS_RESULT_LENGTH || BUSINESS_RESULT_UNSAFE_PATTERN.test(text)) return null;
    if (text.split("\n").length > 80 || text.split("\n").some((line) => line.length > 600)) return null;
    return { text };
  }

  function normalizeGuidance(raw) {
    const conversion = raw?.kind === "sql_conversion" && raw?.title === "查询转换检查" &&
      ["needs_input", "converted", "rejected"].includes(raw?.state);
    if (!conversion && (raw?.kind !== "query_outcomes" || raw?.title !== "查询状态说明")) return null;
    const content = normalizeBusinessResult(raw, "completed");
    return content ? { ...content, format: "plain_text", kind: raw.kind, title: raw.title,
      ...(conversion ? {state: raw.state} : {}) } : null;
  }

  function resultLabel(outcome, resultState, guidance = null, queryStatus = "") {
    if (resultState === "clarification_error") return "口径选项展示失败，尚未执行查询";
    if (queryStatus === "empty_result") return "查询成功，当前条件无数据";
    if (queryStatus === "query_rejected") return "查询未执行，后端安全校验已拒绝";
    if (queryStatus === "query_failed") return "查询执行失败";
    if (queryStatus === "query_unavailable") return "查询服务暂时不可用";
    if (queryStatus === "truncated_result") return "查询结果不完整";
    if (queryStatus === "invalid_result") return "查询结果解析失败";
    if (resultState === "presentation_error") return "查询已完成，结果展示异常";
    if (queryStatus === "succeeded") return outcome === "completed"
      ? "查询完成，结果已通过校验" : "查询已返回，业务结果未确认";
    if (outcome === "completed" && resultState === "batch_complete") return "本批已完成，明细尚未全部取回";
    if (outcome === "completed" && resultState === "summary_only") return "范围汇总已完成";
    if (outcome === "completed" && resultState === "explanation" && guidance?.kind === "sql_conversion") {
      return {needs_input: "待补充转换材料，未查询数据", converted: "转换检查完成，未查询数据", rejected: "转换未通过，未查询数据"}[guidance.state] || "未查询数据";
    }
    if (outcome === "blocked" && resultState === "business_definition_unpublished") return "口径待确认，未执行查询";
    if (outcome === "completed" && resultState === "explanation") return "说明已完成，未查询数据";
    if (outcome === "completed" && resultState === "no_data") return "查询成功，当前范围无数据";
    return OUTCOME_LABELS[outcome] || OUTCOME_LABELS.pending;
  }

  function normalizeResultState(outcome, value, queryStatus = "") {
    if (value === "clarification_error") return value;
    if (value === "presentation_error" && (!queryStatus || queryStatus === "succeeded" || queryStatus === "empty_result")) return value;
    if (QUERY_RESULT_STATES.has(queryStatus)) return queryStatus;
    if (outcome === "blocked" && value === "business_definition_unpublished") return value;
    return outcome === "completed" && ["live_success", "no_data", "explanation", "summary_only", "batch_complete"].includes(value) ? value : "";
  }

  function normalizeTableText(value, limit = MAX_BUSINESS_TABLE_TEXT_LENGTH) {
    if (typeof value !== "string") return null;
    const text = value.replace(/[\r\n\t]+/g, " ").replace(/\s+/g, " ").trim();
    if (!text || text.length > limit || BUSINESS_TABLE_UNSAFE_PATTERN.test(text)) return null;
    return text;
  }

  function normalizeTableNumber(value, allowNegative = false) {
    if (typeof value !== "string" && typeof value !== "number") return null;
    const text = String(value).trim();
    const pattern = allowNegative ? /^-?\d+(?:\.\d+)?$/ : /^\d+(?:\.\d+)?$/;
    if (!pattern.test(text) || text.length > 80) return null;
    return text;
  }

  function normalizeTableUnit(value, allowEmpty = false) {
    if ((value === "" || value == null) && allowEmpty) return "";
    if (typeof value !== "string") return null;
    const unit = value.trim();
    return BUSINESS_TABLE_UNITS.has(unit) ? unit : null;
  }

  function normalizeBusinessTable(rawTable, outcome) {
    if (outcome !== "completed" || !rawTable || typeof rawTable !== "object" || Array.isArray(rawTable)) return null;
    if (rawTable.format !== "table" || !Array.isArray(rawTable.columns) || rawTable.columns.length === 0) return null;
    // Generic model plans are normalized by the backend into neutral `col_N`
    // keys and intentionally do not carry the legacy model_verified marker.
    // Keep accepting the marker for persisted/older responses, while using the
    // shape of the host-screened projection for the current protocol.
    const isGenericModelTable = rawTable.table_id === "model_verified"
      || (rawTable.table_id == null
        && rawTable.columns.every((column) => /^col_[0-9]+$/.test(column?.key || "")
          && (column?.kind === "number" || column?.kind === "text")));
    if (isGenericModelTable) {
      const genericKeys = new Set();
      const genericColumns = rawTable.columns.map((column) => {
        const key = typeof column?.key === "string" ? column.key.trim() : "";
        const label = normalizeTableText(column?.label || key, 100);
        if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key) || !label || genericKeys.has(key)) return null;
        genericKeys.add(key);
        const kind = column?.kind === "number" ? "number" : "text";
        const hasUnit = Object.hasOwn(column, "unit");
        if (hasUnit && (kind !== "number" || !MODEL_RESULT_UNITS.has(column.unit))) return null;
        return {key, label, kind, align: kind === "number" ? "right" : "left", signed: kind === "number",
          ...(hasUnit ? {unit: column.unit} : {})};
      });
      const title = normalizeTableText(rawTable.title, 100);
      const caption = rawTable.caption == null || rawTable.caption === "" ? "" : normalizeTableText(rawTable.caption, 180);
      if (genericColumns.some((column) => !column) || genericColumns.length > MAX_BUSINESS_TABLE_COLUMNS || !title || caption == null
        || !Array.isArray(rawTable.rows) || rawTable.rows.length > MAX_BUSINESS_TABLE_ROWS) return null;
      const rows = rawTable.rows.map((rawRow) => {
        if (!rawRow || typeof rawRow !== "object" || Array.isArray(rawRow)) return null;
        const row = {};
        for (const column of genericColumns) {
          if (!Object.hasOwn(rawRow, column.key)) return null;
          const value = rawRow[column.key];
          if (value == null) { row[column.key] = null; continue; }
          const cell = column.kind === "number" ? normalizeTableNumber(value, true) : normalizeTableText(value);
          if (cell == null) return null;
          row[column.key] = cell;
        }
        return row;
      });
      if (rows.some((row) => !row)) return null;
      const pagination = rawTable.pagination == null ? null : normalizePagination(rawTable.pagination, rows.length);
      if (rawTable.pagination != null && !pagination) return null;
      const generic = {table_id: "model_verified", title, caption, emptyLabel: "当前范围没有可展示的数据", columns: genericColumns, rows, summary: [], notes: [], pagination};
      try { return new TextEncoder().encode(JSON.stringify(generic)).length <= MAX_BUSINESS_TABLE_BYTES ? generic : null; } catch (_error) { return null; }
    }
    const so07Keys = "cargo_owner,sales_order_code,order_created_at,customer_code,customer_name,warehouse_code,warehouse_name,sku_code,sku_name,request_quantity,quantity_unit_reference,sales_unit_reference,order_status,order_detail_state";
    const so07 = rawTable.columns.some(column => ["request_quantity", "quantity_unit_reference", "sales_unit_reference", "order_created_at"].includes(column?.key));
    if (so07 ? rawTable.columns.map(column => column?.key).join(",") !== so07Keys : rawTable.columns.length > MAX_BUSINESS_TABLE_COLUMNS) return null;
    const title = normalizeTableText(rawTable.title, 100);
    const caption = rawTable.caption == null || rawTable.caption === "" ? "" : normalizeTableText(rawTable.caption, 180);
    const emptyLabel = normalizeTableText(rawTable.empty_label, 160);
    if (!title || caption == null || !emptyLabel || !Array.isArray(rawTable.rows) || rawTable.rows.length > MAX_BUSINESS_TABLE_ROWS) return null;
    const seenColumns = new Set();
    const columns = rawTable.columns.map((rawColumn) => {
      const key = typeof rawColumn?.key === "string" ? rawColumn.key.trim() : "";
      const definition = BUSINESS_TABLE_COLUMN_DEFS[key];
      const label = typeof rawColumn?.label === "string" ? rawColumn.label.trim() : "";
      const windowMatch = key === "moving_average" && /^([0-9]{1,3})日移动平均$/.exec(label);
      const dynamicTimeSeriesLabel = windowMatch && Number(windowMatch[1]) >= 2 && Number(windowMatch[1]) <= 365;
      const rawUnit = typeof rawColumn?.unit === "string" ? rawColumn.unit.trim() : "";
      if (!definition || (!BUSINESS_TABLE_LABELS.has(label) && !dynamicTimeSeriesLabel) || seenColumns.has(key)
        || (Object.hasOwn(rawColumn || {}, "unit") && (!rawUnit || !MODEL_RESULT_UNITS.has(rawUnit)))) return null;
      seenColumns.add(key);
      return { key, ...definition, ...(rawUnit ? {unit: rawUnit} : {}),
        ...(rawColumn.nullable === true ? {nullable: true} : {}),
        ...(rawColumn.signed === true && definition.kind === "number" ? {signed: true} : {}),
        ...(so07 && ["sku_code", "sku_name"].includes(key) ? {nullable: true} : {}), label };
    });
    if (columns.some((column) => !column)) return null;
    const rows = rawTable.rows.map((rawRow) => {
      if (!rawRow || typeof rawRow !== "object" || Array.isArray(rawRow)) return null;
      const row = {};
      for (const column of columns) {
        if (column.nullable && Object.hasOwn(rawRow, column.key) && rawRow[column.key] === null) {
          row[column.key] = null;
          continue;
        }
        if (so07 && column.key === "request_quantity" && typeof rawRow[column.key] !== "string") return null;
        const value = column.kind === "number"
          ? normalizeTableNumber(rawRow[column.key], column.signed === true)
          : column.key === "unit"
            ? normalizeTableUnit(rawRow[column.key])
            : normalizeTableText(rawRow[column.key]);
        if (value == null) return null;
        row[column.key] = value;
      }
      return row;
    });
    if (rows.some((row) => !row)) return null;
    const rawSummary = rawTable.summary == null ? [] : rawTable.summary;
    if (!Array.isArray(rawSummary) || rawSummary.length > 6) return null;
    if (so07 && (rawSummary.length !== 1 || rawSummary[0]?.label !== "订单数" || rawSummary[0]?.unit !== "单"
      || typeof rawSummary[0]?.value !== "string" || !/^[0-9]{1,10}$/.test(rawSummary[0].value))) return null;
    const summary = rawSummary.map((rawItem) => {
      if (!rawItem || typeof rawItem !== "object" || Array.isArray(rawItem)) return null;
      const label = typeof rawItem.label === "string" ? rawItem.label.trim() : "";
      if (!BUSINESS_TABLE_SUMMARY_LABELS.has(label)) return null;
      const value = normalizeTableNumber(rawItem.value) || normalizeTableText(rawItem.value, 80);
      const unit = normalizeTableUnit(rawItem.unit, true);
      if (value == null || unit == null) return null;
      return { label, value, unit };
    });
    if (summary.some((item) => !item)) return null;
    const rawNotes = rawTable.notes == null ? [] : rawTable.notes;
    if (!Array.isArray(rawNotes) || rawNotes.length > 3) return null;
    const notes = rawNotes.map((note) => normalizeTableText(note, 240));
    if (notes.some((note) => note == null)) return null;
    const pagination = rawTable.pagination == null ? null : normalizePagination(rawTable.pagination, rows.length);
    if (rawTable.pagination != null && !pagination) return null;
    const normalized = { title, caption, emptyLabel, columns, rows, summary, notes, pagination };
    if (rawTable.detail_batches != null) {
      const meta = normalizeDetailMetadata(rawTable.detail_batches);
      if (!meta || (meta.loaded_rows === 0
        ? columns.map(c => c.key).join(",") !== "quantity,outbound_count" || rows.length !== 1 || pagination
        : columns.map(c => c.key).join(",") !== DETAIL_COLUMNS || !rows.length || (pagination?.total_rows || rows.length) > 5000
          || (pagination?.total_rows || rows.length) > meta.loaded_rows)) return null;
      normalized.detail_batches = meta;
    }
    if (rawTable.drilldown != null) {
      const meta = rawTable.drilldown;
      if (!/^rr_[A-Za-z0-9_-]{32}$/.test(meta.result_id || "") || !Number.isSafeInteger(meta.expires_at) || meta.expires_at < 1
        || columns.map((column) => column.key).join(",") !== RANKING_COLUMNS || pagination || !rows.length || rows.length > 100
        || new Set(rows.map((row) => row.sku_code)).size !== rows.length
        || rows.some((row, index) => row.rank !== String(index + 1))) return null;
      normalized.drilldown = { result_id: meta.result_id, expires_at: meta.expires_at };
    }
    try {
      if (new TextEncoder().encode(JSON.stringify(normalized)).length > MAX_BUSINESS_TABLE_BYTES) return null;
    } catch (_error) {
      return null;
    }
    return normalized;
  }

  function normalizePersistedBusinessTable(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const candidate = value.format === "table"
      ? value
      : { ...value, format: "table", empty_label: value.emptyLabel };
    return normalizeBusinessTable(candidate, "completed");
  }

  function decimalKey(value) {
    const text = normalizeTableNumber(value);
    if (text === null) return null;
    const [whole, fraction = ""] = text.split(".");
    return `${whole.replace(/^0+(?=\d)/, "")}.${fraction.replace(/0+$/, "")}`;
  }

  function validDrilldownTable(table, ranking, skuCode) {
    const ranked = ranking?.drilldown && ranking.rows.find((row) => row.sku_code === skuCode);
    if (!ranked || !table || table.drilldown || table.columns.map((column) => column.key).join(",") !== DRILLDOWN_COLUMNS
      || !table.rows.length || table.rows.some((row) => row.sku_code !== skuCode)) return false;
    return [["实发数量", "quantity", "EA"], ["出库单数", "outbound_count", "单"]].every(([label, key, unit]) => {
      const items = table.summary.filter((item) => item.label === label);
      return items.length === 1 && items[0].unit === unit && decimalKey(items[0].value) === decimalKey(ranked[key]);
    });
  }

  function normalizeStoredDrilldown(raw, ranking, restoring = false) {
    if (!raw || !ranking?.drilldown || !ranking.rows.some((row) => row.sku_code === raw.skuCode)) return null;
    if (!["loading", "interrupted", "completed", "failed"].includes(raw.phase)) return null;
    const requestKey = storedRequestKey(raw.requestKey);
    const table = raw.phase === "completed" ? normalizePersistedBusinessTable(raw.table) : null;
    const valid = raw.phase === "completed" ? validDrilldownTable(table, ranking, raw.skuCode)
      : !["loading", "interrupted"].includes(raw.phase) || Boolean(requestKey);
    return {
      skuCode: raw.skuCode, open: raw.open !== false,
      phase: !valid ? "failed" : restoring && raw.phase === "loading" ? "interrupted" : raw.phase,
      requestKey,
      taskId: /^task_[a-f0-9]{32}$/.test(raw.taskId || "") ? raw.taskId : "",
      errorCode: valid ? storedErrorCode(raw.errorCode) : "query_invalid_result",
      errorStatus: storedErrorStatus(raw.errorStatus), table: valid ? table : null
    };
  }

  function normalizeBusinessTables(value, outcome, persisted = false) {
    if (value == null) return null;
    if (outcome !== "completed" || !Array.isArray(value) || value.length < 1 || value.length > 4) return null;
    const tables = value.map((table) => persisted ? normalizePersistedBusinessTable(table) : normalizeBusinessTable(table, outcome));
    if (tables.some((table) => !table)) return null;
    // Each table has already passed the same host-screened public schema
    // contract. Multi-table rules may add reviewed table families (for
    // example, the center turnover summary plus its warehouse details), so
    // do not maintain a second hard-coded list of only legacy combinations.
    // Keep the bundle guard: duplicate titles or page identities are still
    // rejected rather than silently merging unrelated tables.
    if (new Set(tables.map((table) => table.title)).size !== tables.length) return null;
    const ids = tables.map((table) => table.pagination?.result_id).filter(Boolean);
    return new Set(ids).size === ids.length ? tables : null;
  }

  function normalizePersistedBusinessResult(value) {
    if (typeof value !== "string") return null;
    const text = value.replace(/\r\n?/g, "\n").trim();
    if (!text || text.length > MAX_BUSINESS_RESULT_LENGTH || BUSINESS_RESULT_UNSAFE_PATTERN.test(text)) return null;
    const lines = text.split("\n");
    if (lines.length > 80 || lines.some((line) => line.length > MAX_BUSINESS_RESULT_LENGTH)) return null;
    // New entries use the normal display limits. The wider single-line
    // fallback only keeps histories written by the earlier whitespace-flattening
    // version readable after an upgrade.
    return normalizeBusinessResult({ format: "plain_text", text }, "completed") ||
      { text };
  }

  function normalizeResponse(payload) {
    const parsedClarification = normalizeClarification(payload?.clarification);
    const clarification = parsedClarification?.invalid ? null : parsedClarification;
    const queryStatus = QUERY_RESULT_STATES.has(payload?.query_status)
      ? payload.query_status
      : QUERY_RESULT_STATES.has(payload?.normalized_result_state)
        ? payload.normalized_result_state
        : "";
    let outcome = outcomeKey(parsedClarification?.invalid ? "blocked" : clarification ? "clarification_required" : payload?.outcome?.state || payload?.process?.state || payload?.status);
    if (["query_rejected", "query_failed", "query_unavailable", "truncated_result", "invalid_result"].includes(queryStatus)) {
      outcome = "failed";
    }
    const guidance = outcome === "completed" && payload?.result_state === "explanation" ? normalizeGuidance(payload?.guidance) : null;
    if (payload?.result_state === "explanation" && !guidance) outcome = "blocked";
    const canDisplay = outcome === "completed" && !guidance;
    const businessTables = guidance ? null : normalizeBusinessTables(payload?.business_tables, outcome);
    const businessTable = payload?.business_tables != null ? businessTables?.[0] || null
      : guidance ? null : normalizeBusinessTable(payload?.business_table, outcome);
    const businessResult = guidance ? null : normalizeBusinessResult(payload?.business_result, outcome);
    // Keep server-side validation warnings for audit and quality gates, but do
    // not turn them into a blocking-looking red banner after a usable result.
    const validationWarnings = [];
    const context = payload?.context_state
      ? normalizeContext(payload.context_state)
      : state.contextState || normalizeContext(null);
    const rawArtifacts = Array.isArray(payload?.artifacts)
      ? payload.artifacts
      : payload?.artifact
        ? [payload.artifact]
        : [];
    const artifacts = rawArtifacts.slice(0, 4).map((artifact) => {
      if (!artifact || typeof artifact !== "object") return null;
      const artifactId = boundedText(artifact.artifact_id, 100);
      const url = boundedText(artifact.url, 240);
      if (!REPORT_ARTIFACT_ID_PATTERN.test(artifactId) || artifact.kind !== REPORT_KIND || artifact.content_type !== "image/png") return null;
      try {
        const parsedUrl = new URL(url, window.location.origin);
        if (parsedUrl.origin !== window.location.origin || parsedUrl.pathname !== `/v1/artifacts/${artifactId}` || parsedUrl.search || parsedUrl.hash) return null;
      } catch (_error) {
        return null;
      }
      const title = boundedText(artifact.title, 240);
      const period = boundedText(artifact.period, 180);
      if (!title || !period) return null;
      return { artifact_id: artifactId, kind: REPORT_KIND, content_type: "image/png", title, period, url };
    }).filter(Boolean);
    const report = normalizeReport(payload?.report);
    // Database status is evidence, not browser rendering status. Never turn a
    // rejected presentation into a new SQL failure or silently drop a table.
    const presentationError = canDisplay && (
      payload?.result_state === "presentation_error" ||
      (payload?.business_tables != null && !businessTables) ||
      (payload?.business_table != null && !businessTable) ||
      (payload?.business_result != null && !businessResult) ||
      rawArtifacts.length !== artifacts.length ||
      (queryStatus === "succeeded" && !businessTable && !businessResult && !artifacts.length)
    );
    if (presentationError) outcome = "failed";
    const resultState = parsedClarification?.invalid ? "clarification_error"
      : presentationError ? "presentation_error"
      : normalizeResultState(outcome, payload?.result_state, queryStatus);
    return {
      requestId: boundedText(payload?.request_id, 200),
      outcome,
      resultState,
      outcomeLabel: resultLabel(outcome, resultState, guidance, queryStatus),
      queryStatus,
      clarification,
      context,
      process: guidance || resultState === "clarification_error" ? [] : normalizeProcess(payload?.process, outcome),
      statusMessage: resultState === "clarification_error" ? CLARIFICATION_ERROR_MESSAGE
        : resultState === "presentation_error" ? PRESENTATION_ERROR_MESSAGE
        : (outcome === "completed" ? "" : normalizeStatusMessage(payload?.message)) || ({
        empty_result: "查询成功，但当前条件下没有符合条件的数据。",
        query_rejected: "查询未执行，原因是后端安全校验拒绝了该 SQL。",
        query_failed: "查询执行失败，请稍后重试。",
        query_unavailable: "查询服务暂时不可用，请稍后重试。",
        truncated_result: "查询结果被截断，当前结果不完整，未作为完整结果展示。",
        invalid_result: "Doris 查询已完成，但 Agent 未能解析返回结果，请稍后重试。"
      }[queryStatus] || ""),
      businessResult: canDisplay ? businessResult : null,
      businessTable: canDisplay ? businessTable : null,
      businessTables: canDisplay ? businessTables : null,
      validationWarnings: canDisplay ? validationWarnings : [],
      guidance,
      artifacts: canDisplay ? artifacts : [],
      report: canDisplay ? report : null
    };
  }

  function normalizeReport(raw) {
    if (!raw || typeof raw !== "object" || raw.status !== "completed" || !REPORT_ID_PATTERN.test(raw.report_id || "")) return null;
    const files = Array.isArray(raw.files) ? raw.files.slice(0, 6).map((file) => {
      if (!file || typeof file !== "object" || !REPORT_FILE_ID_PATTERN.test(file.file_id || "") || !REPORT_FILE_FORMATS.has(file.format)) return null;
      const url = boundedText(file.url, 240);
      const filename = boundedText(file.filename, 160);
      const contentType = boundedText(file.content_type, 120);
      try {
        const parsed = new URL(url, window.location.origin);
        if (parsed.origin !== window.location.origin || parsed.pathname !== `/v1/reports/files/${file.file_id}` || parsed.search || parsed.hash) return null;
      } catch (_error) { return null; }
      if (!filename || /[\\/\r\n]/.test(filename) || !contentType) return null;
      return {file_id: file.file_id, report_id: raw.report_id, format: file.format, filename, content_type: contentType, url};
    }).filter(Boolean) : [];
    if (!files.length || files.length !== Math.min(Array.isArray(raw.files) ? raw.files.length : 0, 6)) return null;
    const title = boundedText(raw.title, 160);
    const audience = boundedText(raw.audience, 120);
    const tableCount = Number(raw.table_count);
    const rowCount = Number(raw.row_count);
    if (!title || !audience || !Number.isSafeInteger(tableCount) || tableCount < 1 || !Number.isSafeInteger(rowCount) || rowCount < 1) return null;
    return {status: "completed", report_id: raw.report_id, title, audience, table_count: tableCount, row_count: rowCount, files};
  }

  function uiHistoryStorageKey(conversationId) {
    const value = String(conversationId || "").trim();
    if (!/^[A-Za-z0-9_.:-]{8,200}$/.test(value)) return "";
    return UI_HISTORY_KEY_PREFIX + encodeURIComponent(value);
  }

  function storedTimestamp(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : Date.now();
  }

  function storedRequestKey(value) {
    const candidate = boundedText(value, 240);
    return /^[A-Za-z0-9_.:-]{1,240}$/.test(candidate) ? candidate : "";
  }

  function storedErrorCode(value) {
    const candidate = boundedText(value, 80);
    return /^[A-Za-z0-9_.:-]{1,80}$/.test(candidate) ? candidate : "";
  }

  function storedErrorStatus(value) {
    const status = Number(value);
    return Number.isInteger(status) && status >= 0 && status <= 599 ? status : 0;
  }

  function safeStoredErrorText(value) {
    const text = boundedText(value, 600);
    if (!text || BUSINESS_RESULT_UNSAFE_PATTERN.test(text)) {
      return "本次请求未完成，请稍后重试";
    }
    return text;
  }

  function normalizeStatusMessage(value) {
    const text = boundedText(value, 160);
    return EXECUTION_STATUS_MESSAGES.has(text) ? text : "";
  }

  function normalizeStoredAnswers(rawAnswers, clarification) {
    if (!Array.isArray(rawAnswers) || !clarification?.questions?.length) return null;
    const questions = new Map(clarification.questions.map((question) => [question.id, question]));
    const answers = [];
    rawAnswers.forEach((answer) => {
      const question = questions.get(String(answer?.question_id || "").trim());
      if (!question) return;
      const allowed = new Set(question.options.map((option) => option.id));
      const optionIds = optionIdsFromAnswer(answer).filter((optionId) => allowed.has(optionId));
      if (!optionIds.length) return;
      const range = storedDateRange(answer.date_range);
      answers.push({ question_id: question.id, option_ids: [...new Set(optionIds)],
        ...(DATE_RANGE_OPTIONS[question.id] && optionIds.includes(DATE_RANGE_OPTIONS[question.id]) && range ? {date_range: range} : {}) });
    });
    return answers.length ? answers : null;
  }

  function normalizeStoredEvent(rawEvent, fallbackQuestion = "") {
    if (!rawEvent || typeof rawEvent !== "object") return null;
    const kind = String(rawEvent.kind || "").trim();
    if (kind === "proposal") return proposalReview.restore(rawEvent);
    if (kind === "interpretation") return interpretation.restore(rawEvent);
    const createdAt = storedTimestamp(rawEvent.createdAt);
    const requestKey = storedRequestKey(rawEvent.requestKey);
    const taskId = /^task_[a-f0-9]{32}$/.test(rawEvent.taskId || "") ? rawEvent.taskId : "";
    if (kind === "question") {
      const question = displayQuestionText(rawEvent.question);
      return question ? { kind: "question", question, createdAt } : null;
    }
    if (kind === "running") {
      const ruleSubmission = normalizeRuleSubmission(rawEvent);
      return {
        kind: "error",
        detailBatch: normalizeDetailSelection(rawEvent.detailBatch),
        conversionSubmitted: rawEvent.conversionSubmitted === true,
        taskId,
        requestKey,
        requestId: "",
        question: displayQuestionText(rawEvent.question || fallbackQuestion),
        ...(ruleSubmission || {}),
        text: RELOAD_INTERRUPTED_MESSAGE,
        errorCode: "reload_interrupted",
        errorStatus: 0,
        createdAt
      };
    }
    if (kind === "error") {
      const ruleSubmission = normalizeRuleSubmission(rawEvent);
      return {
        kind: "error",
        detailBatch: normalizeDetailSelection(rawEvent.detailBatch),
        conversionSubmitted: rawEvent.conversionSubmitted === true,
        taskId,
        requestKey,
        requestId: boundedText(rawEvent.requestId, 200),
        question: displayQuestionText(rawEvent.question || fallbackQuestion),
        ...(ruleSubmission || {}),
        text: safeStoredErrorText(rawEvent.text),
        errorCode: storedErrorCode(rawEvent.errorCode),
        errorStatus: storedErrorStatus(rawEvent.errorStatus),
        createdAt
      };
    }
    if (kind === "clarification") {
      const clarification = cloneClarification(rawEvent.clarification);
      if (!clarification) return null;
      clarification.token = "";
      return {
        kind: "clarification",
        requestKey,
        clarification,
        sourceQuestion: displayQuestionText(rawEvent.sourceQuestion || fallbackQuestion),
        context: normalizeContext(rawEvent.context),
        selectedAnswers: normalizeStoredAnswers(rawEvent.selectedAnswers, clarification),
        submitting: false,
        selectionConfirmed: rawEvent.selectionConfirmed === true,
        createdAt
      };
    }
    if (kind !== "execution") return null;
    let outcome = outcomeKey(rawEvent.outcome);
    if (outcome === "running") {
      return {
        kind: "error",
        requestKey,
        requestId: "",
        question: displayQuestionText(rawEvent.question || fallbackQuestion),
        text: RELOAD_INTERRUPTED_MESSAGE,
        errorCode: "reload_interrupted",
        errorStatus: 0,
        createdAt
      };
    }
    if (!["completed", "failed", "blocked"].includes(outcome)) return null;
    const normalizedBusinessResult = normalizePersistedBusinessResult(rawEvent.businessResult?.text);
    const normalizedBusinessTable = normalizePersistedBusinessTable(rawEvent.businessTable);
    // Older clients persisted a local table rejection as a query failure while
    // retaining a verified report. Recover that report without running SQL again.
    const legacyReport = outcome === "failed" && rawEvent.queryStatus === "succeeded" &&
      rawEvent.resultState !== "presentation_error" && rawEvent.businessTable == null && rawEvent.businessTables == null &&
      (!rawEvent.statusMessage || rawEvent.statusMessage === "本次未形成可确认的实时数据结果，请重新提交") &&
      normalizeStoredArtifacts(rawEvent.artifacts).length > 0;
    if (legacyReport || rawEvent.resultState === "presentation_error") outcome = "completed";
    const normalized = normalizeResponse({
      request_id: rawEvent.requestId,
      outcome: { state: outcome },
      result_state: legacyReport ? "live_success" : rawEvent.resultState,
      query_status: rawEvent.queryStatus,
      guidance: rawEvent.guidance,
      context_state: rawEvent.context,
      process: rawEvent.process,
      message: rawEvent.statusMessage,
      business_result: normalizedBusinessResult
        ? { format: "plain_text", text: normalizedBusinessResult.text }
        : null,
      business_table: normalizedBusinessTable
        ? { ...normalizedBusinessTable, format: "table", empty_label: normalizedBusinessTable.emptyLabel }
        : rawEvent.businessTable,
      business_tables: rawEvent.businessTables == null ? null : Array.isArray(rawEvent.businessTables)
        ? rawEvent.businessTables.map((table) => ({...table, format: "table", empty_label: table?.emptyLabel})) : [],
      validation_warnings: rawEvent.validationWarnings,
      artifacts: rawEvent.artifacts,
      report: rawEvent.report
    });
    const feedbackType = ["accepted", "correction"].includes(rawEvent.feedback?.type)
      ? rawEvent.feedback.type
      : null;
    return {
      kind: "execution",
      requestKey,
      requestId: normalized.requestId,
      question: displayQuestionText(rawEvent.question || fallbackQuestion),
      ...(normalizeRuleSubmission(rawEvent) || {}),
      outcome: normalized.outcome,
      resultState: normalized.resultState,
      queryStatus: normalized.queryStatus,
      outcomeLabel: normalized.outcomeLabel,
      process: normalized.process,
      statusMessage: normalized.statusMessage,
      context: normalized.context,
      businessResult: normalized.businessResult,
      businessTable: normalized.businessTable,
      businessTables: normalized.businessTables,
      validationWarnings: normalized.validationWarnings,
      drilldown: normalizeStoredDrilldown(rawEvent.drilldown, normalized.businessTable, true),
      guidance: normalized.guidance,
      artifacts: normalized.artifacts,
      report: normalized.report,
      feedback: feedbackType ? { type: feedbackType, issueType: null } : null,
      createdAt
    };
  }

  function normalizeStoredSession(rawSession, index) {
    if (!rawSession || typeof rawSession !== "object") return null;
    const id = boundedText(rawSession.id, 120);
    if (!/^[A-Za-z0-9_-]{1,120}$/.test(id)) return null;
    const title = boundedText(rawSession.title, 80) || "取数会话 " + (index + 1);
    const rawEvents = Array.isArray(rawSession.events) ? rawSession.events : [];
    const events = [];
    let fallbackQuestion = "";
    rawEvents.forEach((rawEvent) => {
      const event = normalizeStoredEvent(rawEvent, fallbackQuestion);
      if (!event) return;
      events.push(event);
      if (event.kind === "question") fallbackQuestion = event.question;
    });
    const reportKind = rawSession.reportKind === REPORT_KIND ? REPORT_KIND : null;
    const validProfiles = new Set([
      "available_plus_transit_finished_goods",
      "actual_finished_goods",
      "record_deduction_all"
    ]);
    return {
      id,
      title,
      createdAt: storedTimestamp(rawSession.createdAt),
      events,
      contextState: normalizeContext(rawSession.contextState),
      pendingClarification: null,
      reportKind,
      reportProfileId: validProfiles.has(rawSession.reportProfileId)
        ? rawSession.reportProfileId
        : null
    };
  }

  function normalizeStoredArtifacts(rawArtifacts) {
    return normalizeResponse({
      outcome: { state: "completed" },
      artifacts: rawArtifacts
    }).artifacts;
  }

  function serializeUiEvent(event) {
    if (!event || typeof event !== "object") return null;
    const kind = String(event.kind || "").trim();
    if (kind === "proposal") return proposalReview.serialize(event);
    if (kind === "interpretation") return interpretation.serialize(event);
    const createdAt = storedTimestamp(event.createdAt);
    const requestKey = storedRequestKey(event.requestKey);
    const taskId = /^task_[a-f0-9]{32}$/.test(event.taskId || "") ? event.taskId : "";
    if (kind === "question") {
      const question = displayQuestionText(event.question);
      return question ? { kind: "question", question, createdAt } : null;
    }
    if (kind === "running") {
      const ruleSubmission = normalizeRuleSubmission(event);
      return {
        kind: "running",
        detailBatch: normalizeDetailSelection(event.detailBatch),
        conversionSubmitted: event.conversionSubmitted === true,
        taskId,
        requestKey,
        question: displayQuestionText(event.question),
        ...(ruleSubmission || {}),
        createdAt
      };
    }
    if (kind === "error") {
      const ruleSubmission = normalizeRuleSubmission(event);
      return {
        kind: "error",
        detailBatch: normalizeDetailSelection(event.detailBatch),
        conversionSubmitted: event.conversionSubmitted === true,
        taskId,
        requestKey,
        requestId: boundedText(event.requestId, 200),
        question: displayQuestionText(event.question),
        ...(ruleSubmission || {}),
        text: safeStoredErrorText(event.text),
        errorCode: storedErrorCode(event.errorCode),
        errorStatus: storedErrorStatus(event.errorStatus),
        createdAt
      };
    }
    if (kind === "clarification") {
      const clarification = cloneClarification(event.clarification);
      if (!clarification) return null;
      clarification.token = "";
      return {
        kind: "clarification",
        requestKey,
        clarification,
        sourceQuestion: displayQuestionText(event.sourceQuestion),
        context: normalizeContext(event.context),
        selectedAnswers: normalizeStoredAnswers(event.selectedAnswers, clarification),
        selectionConfirmed: event.selectionConfirmed === true,
        createdAt
      };
    }
    if (kind !== "execution") return null;
    const outcome = outcomeKey(event.outcome);
    if (!["completed", "failed", "blocked"].includes(outcome)) return null;
    return {
      kind: "execution",
      requestKey,
      requestId: boundedText(event.requestId, 200),
      question: displayQuestionText(event.question),
      ...(normalizeRuleSubmission(event) || {}),
      outcome,
      resultState: normalizeResultState(outcome, event.resultState, event.queryStatus),
      queryStatus: QUERY_RESULT_STATES.has(event.queryStatus) ? event.queryStatus : "",
      process: event.resultState === "explanation" ? [] : normalizeProcess(event.process, outcome),
      statusMessage: normalizeStatusMessage(event.statusMessage),
      context: normalizeContext(event.context),
      businessResult: normalizePersistedBusinessResult(event.businessResult?.text),
      businessTable: normalizePersistedBusinessTable(event.businessTable),
      businessTables: normalizeBusinessTables(event.businessTables, event.resultState === "presentation_error" ? "completed" : outcome, true),
      drilldown: normalizeStoredDrilldown(event.drilldown, normalizePersistedBusinessTable(event.businessTable)),
      guidance: normalizeGuidance(event.guidance),
      artifacts: normalizeStoredArtifacts(event.artifacts),
      report: normalizeReport(event.report),
      feedback: ["accepted", "correction"].includes(event.feedback?.type)
        ? { type: event.feedback.type, issueType: null }
        : null,
      createdAt
    };
  }

  function serializeUiSession(session, eventLimit) {
    const rawEvents = Array.isArray(session?.events) ? session.events : [];
    let selectedEvents = rawEvents;
    if (selectedEvents.length > eventLimit) {
      selectedEvents = selectedEvents.slice(-eventLimit);
      if (selectedEvents[0]?.kind !== "question") {
        const firstQuestion = rawEvents
          .slice(0, rawEvents.length - eventLimit)
          .reverse()
          .find((event) => event?.kind === "question");
        if (firstQuestion) selectedEvents = [firstQuestion, ...selectedEvents].slice(-eventLimit);
      }
    }
    return {
      id: boundedText(session?.id, 120),
      title: boundedText(session?.title, 80),
      createdAt: storedTimestamp(session?.createdAt),
      events: selectedEvents.map(serializeUiEvent).filter(Boolean),
      contextState: normalizeContext(session?.contextState),
      reportKind: session?.reportKind === REPORT_KIND ? REPORT_KIND : null,
      reportProfileId: ["available_plus_transit_finished_goods", "actual_finished_goods", "record_deduction_all"]
        .includes(session?.reportProfileId)
        ? session.reportProfileId
        : null
    };
  }

  function buildUiHistorySnapshot(sessionLimit, eventLimit) {
    return {
      version: UI_HISTORY_VERSION,
      activeId: boundedText(state.activeId, 120),
      nextSessionNumber: Number.isFinite(Number(state.nextSessionNumber))
        ? Math.max(1, Math.floor(Number(state.nextSessionNumber)))
        : 1,
      sessions: state.sessions
        .slice(0, sessionLimit)
        .map((session) => serializeUiSession(session, eventLimit))
        .filter((session) => session.id)
    };
  }

  function historyByteLength(value) {
    try {
      return new TextEncoder().encode(value).length;
    } catch (_error) {
      return value.length;
    }
  }

  function persistUiHistory() {
    if (!state.historyStorageKey) return;
    try {
      for (const [sessionLimit, eventLimit] of [[40, 80], [20, 40], [10, 20], [5, 10]]) {
        const encoded = JSON.stringify(buildUiHistorySnapshot(sessionLimit, eventLimit));
        if (historyByteLength(encoded) <= MAX_PERSISTED_HISTORY_BYTES || sessionLimit === 5) {
          localStorage.setItem(state.historyStorageKey, encoded);
          return;
        }
      }
    } catch (_error) {
      // Browser storage is an enhancement; the server conversation remains authoritative.
    }
  }

  function restoreUiHistory(conversationId) {
    state.historyStorageKey = uiHistoryStorageKey(conversationId);
    if (!state.historyStorageKey) return false;
    let payload;
    try {
      const encoded = localStorage.getItem(state.historyStorageKey);
      if (!encoded) return false;
      payload = JSON.parse(encoded);
    } catch (_error) {
      return false;
    }
    if (
      !payload ||
      payload.version !== UI_HISTORY_VERSION ||
      !Array.isArray(payload.sessions)
    ) {
      return false;
    }
    const sessions = payload.sessions
      .slice(0, MAX_PERSISTED_SESSIONS)
      .map(normalizeStoredSession)
      .filter(Boolean);
    if (!sessions.length) return false;
    state.sessions = sessions;
    const requestedActiveId = boundedText(payload.activeId, 120);
    state.activeId = sessions.some((session) => session.id === requestedActiveId)
      ? requestedActiveId
      : sessions[0].id;
    const storedNext = Number(payload.nextSessionNumber);
    state.nextSessionNumber = Number.isFinite(storedNext) && storedNext > 0
      ? Math.min(Math.floor(storedNext), 1000000)
      : sessions.length + 1;
    return true;
  }

  function clearPersistedUiHistory() {
    if (!state.historyStorageKey) return;
    try { localStorage.removeItem(state.historyStorageKey); } catch (_error) { /* optional */ }
  }

  function renderSidebar() {
    const sessions = state.sessions;
    elements.conversationList.innerHTML = sessions.map((session) => (
      (() => {
        const latest = [...(session.events || [])].reverse().find((event) => event?.kind === "running" || event?.kind === "execution" || event?.kind === "error");
        const status = latest?.kind === "running" ? "running" : latest?.outcome === "completed" ? "completed" : latest ? "attention" : "idle";
        const label = {running: "执行中", completed: "已完成", attention: "需关注", idle: "未开始"}[status];
        return `<div class="conversation-item-row">` +
      `<button class="conversation-item ${session.id === state.activeId ? "active" : ""}" type="button" data-session-id="${escapeHtml(session.id)}" ${session.id === state.activeId ? 'aria-current="page"' : ""}>` +
      `<span class="conversation-status-dot ${status}" title="${label}" aria-label="${label}"></span>` +
      `<span class="conversation-item-title">${escapeHtml(session.title)}</span>` +
      `<span class="conversation-item-date">${formatTime(session.createdAt)}</span></button>` +
      `<button class="conversation-delete-button" type="button" data-delete-session-id="${escapeHtml(session.id)}" title="删除此界面会话" aria-label="删除${escapeHtml(session.title)}">` +
      `<svg class="icon" aria-hidden="true"><use href="#icon-trash" /></svg></button></div>`;
      })()
    )).join("");
    elements.conversationEmpty.hidden = sessions.length > 0;
    elements.clearHistoryButton.disabled = state.loading || hasAnyPendingClarification() || !activeSession().events.length;
    elements.newChatButton.disabled = !state.sessionReady;
    elements.definitionButton.disabled = !state.sessionReady || state.feedbackSubmitting;
    elements.learningButton.disabled = !state.sessionReady;
    renderLearningSummary(state.learningSummary);
    elements.conversationList.querySelectorAll("[data-session-id]").forEach((button) => {
      button.addEventListener("click", () => {
        clearTransientInputs();
        state.activeId = button.dataset.sessionId;
        closeSidebar();
        resetInspector();
        render();
        restoreInspectorFromSession();
        updateComposerState();
        void proposalReview.resumeSession(activeSession());
        void interpretation.resumeSession(activeSession());
      });
    });
    elements.conversationList.querySelectorAll("[data-delete-session-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteUiSession(button.dataset.deleteSessionId);
      });
    });
  }

  async function deleteUiSession(sessionId) {
    if (state.loading || !sessionId) return;
    if (hasAnyPendingClarification()) {
      showToast("请先完成当前口径选择");
      return;
    }
    let index = state.sessions.findIndex((session) => session.id === sessionId);
    if (index < 0) return;
    const session = state.sessions[index];
    if (!await proposalReview.closeSession(session)) return;
    index = state.sessions.indexOf(session);
    if (index < 0) return;
    const wasActive = state.activeId === sessionId;
    if (wasActive) clearTransientInputs();
    state.sessions.splice(index, 1);
    if (!state.sessions.length) {
      const replacement = createSession();
      state.sessions.push(replacement);
      state.activeId = replacement.id;
    } else if (wasActive) {
      state.activeId = state.sessions[Math.min(index, state.sessions.length - 1)].id;
    }
    resetInspector();
    render();
    updateComposerState();
    showToast("已删除此界面会话记录");
  }

  function assistantAvatarMarkup() {
    return `<img class="message-avatar-image" src="${TIGER_AVATAR_SRC}" width="29" height="29" loading="lazy" alt="" />`;
  }

  function renderStageList(steps, className = "inline-process") {
    return `<ol class="${className}">${steps.map((step) => (
      `<li class="stage-${escapeHtml(step.state)}"><span class="stage-marker"><svg class="icon" aria-hidden="true"><use href="#${statusIcon(step.state)}" /></svg></span><span>${escapeHtml(stageLabel(step))}</span></li>`
    )).join("")}</ol>`;
  }

  function statusIcon(status) {
    if (["failed", "blocked", "cancelled"].includes(status)) return "icon-close";
    return status === "completed" ? "icon-check" : "icon-clock";
  }

  function contextMarkup(context) {
    // Responses from older gateways may carry the raw lifecycle shape. Keep
    // the public strip stable instead of rendering undefined labels.
    const value = context && typeof context.contextLabel === "string"
      ? context
      : normalizeContext(context);
    return `<div class="context-strip"><svg class="icon" aria-hidden="true"><use href="#icon-layers" /></svg><span>上下文${escapeHtml(value.contextLabel)} · ${escapeHtml(value.compressionLabel)}</span></div>`;
  }

  function renderUserQuestion(event) {
    const question = displayQuestionText(event?.question);
    if (!question) return "";
    return `<article class="message user-question-message" aria-label="我的取数问题">` +
      `<div class="message-body user-question-body"><div class="message-meta user-question-meta">我的取数问题</div>` +
      `<div class="user-question-bubble">${escapeHtml(question)}</div></div></article>`;
  }

  function renderClarification(event, index) {
    const clarification = event.clarification;
    const questions = clarification?.questions || [];
    const disabled = event.submitting || state.loading ? " disabled" : "";
    const retainedSelection = Array.isArray(event.selectedAnswers) ? event.selectedAnswers : [];
    const cardTitle = event.selectionConfirmed ? "已保留本次口径选择" : "请一次完成以下口径选择";
    const buttonLabel = event.submitting ? "正在确认…" : event.selectionConfirmed ? "重试执行" : "确认并继续";
    return `<article class="message assistant clarification-message" data-clarification-index="${index}">` +
      `<div class="message-avatar" aria-hidden="true">${assistantAvatarMarkup()}</div>` +
      `<div class="message-body${questions.some((question) => DATE_RANGE_OPTIONS[question.id]) ? " clarification-body-wide" : ""}"><div class="message-meta">皇家小虎取数运行时 · 需要确认</div>` +
      `<div class="clarification-card"><div class="clarification-title">${cardTitle}</div>` +
      `<div class="clarification-list">${questions.map((question) => (
        `<fieldset class="clarification-question"><legend>${escapeHtml(question.prompt)}</legend>` +
        `<div class="choice-list">${question.options.map((option) => {
          const inputType = question.multiple ? "checkbox" : "radio";
          const name = `choice_${escapeHtml(question.id)}`;
          const checked = answerHasOption(retainedSelection, question.id, option.id) ? " checked" : "";
          const unavailable = option.available === false;
          const optionDisabled = disabled || unavailable ? " disabled" : "";
          return `<label class="choice-option${unavailable ? " choice-option-unavailable" : ""}"><input type="${inputType}" name="${name}" data-question-id="${escapeHtml(question.id)}" data-option-id="${escapeHtml(option.id)}"${checked}${optionDisabled} /><span>${escapeHtml(option.label)}</span></label>`;
        }).join("")}</div>${renderClarificationDates(question, retainedSelection, disabled, index)}</fieldset>`
      )).join("")}</div>` +
      `<p id="clarification_error_${index}" class="clarification-error" role="alert" hidden></p>` +
      `<button class="primary-button clarification-submit" type="button" data-clarification-submit="${index}"${disabled}>` +
      `<svg class="icon" aria-hidden="true"><use href="#icon-check" /></svg>${buttonLabel}</button>` +
      `</div>${contextMarkup(event.context)}</div></article>`;
  }

  function storedDateRange(value) {
    if (!value || typeof value !== "object") return null;
    const clean = (part) => typeof part === "string" && /^\d{4}-\d{2}-\d{2}$/.test(part) ? part : "";
    return {start: clean(value.start), end: clean(value.end)};
  }

  function renderClarificationDates(question, answers, disabled, index) {
    const option = DATE_RANGE_OPTIONS[question.id];
    if (!option) return "";
    const active = answerHasOption(answers, question.id, option);
    const range = storedDateRange(answers.find((answer) => answer.question_id === question.id)?.date_range) || {};
    const inputDisabled = disabled || (!active ? " disabled" : "");
    return `<div class="clarification-dates" data-date-range="${question.id}"${active ? "" : " hidden"}>` +
      [["start", "开始日期（含）"], ["end", "结束日期（不含）"]].map(([key, label]) => {
        const id = `range_${index}_${question.id}_${key}`;
        return `<div class="clarification-date"><label for="${id}">${label}</label>` +
          `<input id="${id}" type="date" data-date-question="${question.id}" data-date-part="${key}" value="${escapeHtml(range[key] || "")}" required${inputDisabled} /></div>`;
      }).join("") + `</div>`;
  }

  function readClarificationChoices(cardRoot, clarification) {
    return clarification.questions.flatMap((question) => {
      const options = [...cardRoot.querySelectorAll("input[data-question-id]:checked")]
        .filter((input) => input.dataset.questionId === question.id).map((input) => input.dataset.optionId);
      if (!options.length) return [];
      const answer = {question_id: question.id, option_ids: [...new Set(options)]};
      if (options.includes(DATE_RANGE_OPTIONS[question.id])) {
        answer.date_range = Object.fromEntries([...cardRoot.querySelectorAll("input[data-date-question]")]
          .filter((input) => input.dataset.dateQuestion === question.id).map((input) => [input.dataset.datePart, input.value]));
      }
      return [answer];
    });
  }

  function renderArtifacts(artifacts) {
    if (!Array.isArray(artifacts) || !artifacts.length) return "";
    return `<div class="artifact-list">${artifacts.map((artifact) => (
      `<figure class="report-artifact"><div class="report-artifact-frame"><img src="${escapeHtml(artifact.url)}" alt="${escapeHtml(artifact.title)}" loading="lazy" /></div>` +
      `<figcaption><div><strong>${escapeHtml(artifact.title)}</strong><span>${escapeHtml(artifact.period)}</span></div>` +
      `<a class="artifact-download" href="${escapeHtml(artifact.url)}" download="royal-tiger-turnover.png" title="下载数据图片" aria-label="下载数据图片"><svg class="icon" aria-hidden="true"><use href="#icon-download" /></svg></a></figcaption></figure>`
    )).join("")}</div>`;
  }

  function renderReport(event, index) {
    const hasRows = (event?.businessTables || [event?.businessTable]).some((table) => table && table.rows?.length);
    // Generic report actions live beside the table export controls. Keep this
    // fallback only for legacy result cards that have report files but no table.
    if (event?.report?.files?.length && !hasRows) {
      return `<div class="report-downloads" role="group" aria-label="下载分析报告"><span class="report-download-label">分析报告</span>${event.report.files.map((file) => `<a class="report-download-link" href="${escapeHtml(file.url)}" download="${escapeHtml(file.filename)}" title="下载${escapeHtml(file.format)}报告"><svg class="icon" aria-hidden="true"><use href="#icon-download" /></svg>${escapeHtml(({excel:"Excel",word:"Word",csv:"CSV",png:"PNG",pdf:"PDF",html:"网页"})[file.format] || file.format)}</a>`).join("")}</div>`;
    }
    return "";
  }

  function renderTableReportActions(eventIndex, tableIndex, hasRows) {
    if (eventIndex < 0 || tableIndex !== 0) return "";
    const event = activeSession()?.events[eventIndex];
    if (!event) return "";
    const files = event.report?.files || [];
    if (files.length) {
      return `<span class="table-export-divider" aria-hidden="true"></span><label class="sr-only" for="report-instructions-${eventIndex}">报告要求</label><input id="report-instructions-${eventIndex}" name="report-instructions-${eventIndex}" type="text" autocomplete="off" class="table-report-instructions" data-report-instructions="${eventIndex}" maxlength="2000" placeholder="重新生成报告要求（可选）…"${reportJobs.has(event.requestId) ? " disabled" : ""}><span class="table-export-label">报告</span>` +
        files.map((file) => `<a class="table-export-button table-report-link" href="${escapeHtml(file.url)}" download="${escapeHtml(file.filename)}" title="下载${escapeHtml(file.format)}报告"><svg class="icon" aria-hidden="true"><use href="#icon-download" /></svg>${escapeHtml(({excel:"Excel",word:"Word",csv:"CSV",png:"PNG",pdf:"PDF",html:"网页"})[file.format] || file.format)}</a>`).join("") +
        `<button type="button" class="table-export-button table-report-regenerate" data-generate-report="${eventIndex}"${reportJobs.has(event.requestId) ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#icon-chart" /></svg>${reportJobs.has(event.requestId) ? "正在生成" : "重新生成"}</button>`;
    }
    if (event.outcome !== "completed" || event.queryStatus !== "succeeded"
      || !["succeeded", "live_success", "summary_only", "batch_complete"].includes(event.resultState) || !hasRows) return "";
    const busy = reportJobs.has(event.requestId);
    return `<span class="table-export-divider" aria-hidden="true"></span><label class="sr-only" for="report-instructions-${eventIndex}">报告要求</label><input id="report-instructions-${eventIndex}" name="report-instructions-${eventIndex}" type="text" autocomplete="off" class="table-report-instructions" data-report-instructions="${eventIndex}" maxlength="2000" placeholder="报告要求（可选）…"${busy ? " disabled" : ""}><button type="button" class="table-export-button table-report-button" data-generate-report="${eventIndex}"${busy ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#icon-chart" /></svg>${busy ? "正在生成报告" : "生成报告"}</button>`;
  }

  function formatTableNumber(value) {
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

  function formatTableCount(value) {
    const text = String(value ?? "").trim();
    return /^-?\d+$/.test(text)
      ? text.replace(/\B(?=(\d{3})+(?!\d))/g, ",")
      : text;
  }

  function drilldownError(detail) {
    if (detail.errorCode === "semantic_query_error") return "明细未通过完整性或对账校验，未展示数据；原排名已保留。";
    return safeErrorMessage(detail.errorCode, detail.errorStatus);
  }

  function renderDrilldown(event) {
    const detail = event.drilldown;
    if (!detail?.open || !event.businessTable?.drilldown) return "";
    const loading = ["loading", "interrupted"].includes(detail.phase);
    const failed = detail.phase === "failed";
    const retry = failed && !["ranking_unavailable", "session_security_error"].includes(detail.errorCode);
    return `<section class="drilldown-panel" data-drilldown-panel="${event.index}" aria-label="SKU 出库明细" aria-busy="${loading}">` +
      `<div class="drilldown-heading"><button type="button" class="icon-button drilldown-back" data-drilldown-close="${event.index}" title="返回排名" aria-label="返回排名"><svg class="icon" aria-hidden="true"><use href="#icon-chevron" /></svg></button>` +
      `<h3 tabindex="-1">SKU ${escapeHtml(detail.skuCode)}</h3><span class="drilldown-state" role="status">${loading ? "正在查询并对账" : failed ? "明细未完成" : "明细对账完成"}</span></div>` +
      `${failed ? `<p class="drilldown-error" role="alert">${escapeHtml(drilldownError(detail))}</p>` : ""}` +
      `${retry ? `<button type="button" class="retry-button" data-drilldown-retry="${event.index}"><svg class="icon" aria-hidden="true"><use href="#icon-retry" /></svg>重试明细</button>` : ""}` +
      `${detail.phase === "completed" && detail.table ? renderBusinessTable(detail.table, event.index, detail, -1) : ""}</section>`;
  }

  async function openDrilldown(session, event, skuCode, retry = false, resume = false) {
    const ranking = event?.businessTable;
    if (!state.sessionReady || !ranking?.drilldown || !ranking.rows.some((row) => row.sku_code === skuCode)) return;
    const previous = event.drilldown;
    if (previous && drilldownJobs.has(previous)) return;
    if (!retry && !resume && previous?.skuCode === skuCode && previous.phase === "completed") {
      previous.open = true; persistUiHistory(); renderMessages(); return;
    }
    const replay = previous?.requestKey && (resume || (retry && REPLAYABLE_ERROR_CODES.has(previous.errorCode)));
    const detail = {skuCode, open: resume ? previous?.open !== false : true, phase:"loading",
      requestKey: replay ? previous.requestKey : randomId("req"), taskId: replay ? previous.taskId : "",
      errorCode:"", errorStatus:0, table:null};
    event.drilldown = detail;
    const conversation = state.backendConversationId;
    const current = () => state.backendConversationId === conversation && state.sessions.includes(session) && session.events.includes(event) && event.drilldown === detail;
    const repaint = (focus = false) => {
      if (!current()) return;
      persistUiHistory();
      if (activeSession() === session) {
        const panelSelector = `[data-drilldown-panel="${session.events.indexOf(event)}"]`;
        const hadFocus = elements.messageList.querySelector(panelSelector)?.contains(document.activeElement);
        renderMessages();
        if ((focus || hadFocus) && detail.open) elements.messageList.querySelector(`${panelSelector} h3`)?.focus({preventScroll:true});
      }
    };
    drilldownJobs.add(detail); repaint(true);
    try {
      // A completed receipt may remain valid after the ranking context expires.
      if (!replay && ranking.drilldown.expires_at * 1000 <= Date.now()) throw new ApiError("", "ranking_unavailable", 410);
      const selection = {result_id:ranking.drilldown.result_id, sku_code:skuCode};
      const payload = await postChatRequest({conversation_id:conversation, drilldown:selection}, detail.requestKey, detail.taskId, (task) => {
        if (!current()) return false;
        if (!/^task_[a-f0-9]{32}$/.test(task?.task_id || "")) throw new ApiError("", "invalid_response", 502);
        if (detail.taskId && detail.taskId !== task.task_id) throw new ApiError("", "invalid_response", 502);
        if (!["queued", "running", "completed", "failed", "interrupted", "cancelled"].includes(task.state)
          || task.state === "completed" && (!Number.isInteger(task.http_status) || task.http_status < 200 || task.http_status >= 300)
          || task.state === "failed" && (!Number.isInteger(task.http_status) || task.http_status < 400)) throw new ApiError("", "invalid_response", 502);
        detail.taskId = task.task_id;
        persistUiHistory();
        return true;
      });
      if (!current()) return;
      ensureSharedConversation(payload);
      const table = normalizeBusinessTable(payload.business_table, "completed");
      if (payload.result_state !== "live_success" || payload.outcome?.state !== "completed"
        || payload.drilldown_source?.result_id !== selection.result_id || payload.drilldown_source?.sku_code !== skuCode
        || !validDrilldownTable(table, ranking, skuCode)) throw new ApiError("", "query_invalid_result", 502);
      detail.table = table;
      detail.phase = "completed";
    } catch (error) {
      if (!current()) return;
      detail.phase = "failed";
      detail.errorCode = error instanceof ApiError ? error.code : "invalid_response";
      detail.errorStatus = error instanceof ApiError ? error.status : 502;
    } finally {
      drilldownJobs.delete(detail);
      repaint();
    }
  }

  function resumeInterruptedDrilldowns() {
    for (const session of state.sessions) {
      for (const event of session.events) {
        if (event.drilldown?.phase === "interrupted") void openDrilldown(session, event, event.drilldown.skuCode, false, true);
      }
    }
  }

  function renderBusinessTable(table, eventIndex = -1, detailState = null, tableIndex = 0) {
    const isRanking = table.columns.some((column) => column.key === "rank");
    const countLabel = table.pagination ? `共 ${table.pagination.total_rows} 行 · 已对账` : table.rows.length ? `共 ${table.rows.length} 行` : "暂无数据";
    const summary = table.summary.length
      ? `<div class="business-result-summary" role="list">${table.summary.map((item) => (
        `<div class="business-summary-item" role="listitem"><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(formatTableNumber(item.value))}</strong>${item.unit ? `<em>${escapeHtml(item.unit)}</em>` : ""}</div>`
      )).join("")}</div>`
      : "";
    const header = table.columns.map((column) => (
      `<th scope="col" class="table-column-${column.key} table-align-${column.align}"><span>${escapeHtml(column.label)}</span>${column.unit ? `<small>${escapeHtml(column.unit)}</small>` : ""}</th>`
    )).join("");
    const body = table.rows.length
      ? table.rows.map((row) => `<tr>${table.columns.map((column) => {
        const rawValue = row[column.key];
        const value = rawValue === null ? "暂无" : column.kind === "number" ? formatTableNumber(rawValue) : rawValue;
        const unit = rawValue === null ? "" : column.unit || "";
        const inlineSku = isRanking && column.key === "sku_name" && row.sku_code
          ? `<small class="mobile-inline-sku">SKU ${escapeHtml(row.sku_code)}</small>`
          : "";
        const content = `<span>${escapeHtml(value)}</span>${inlineSku}`;
        const clickable = table.drilldown && eventIndex >= 0 && column.key === "sku_name";
        const cell = clickable ? `<button type="button" class="sku-drilldown" data-drilldown-index="${eventIndex}" data-drilldown-sku="${escapeHtml(row.sku_code)}" aria-label="查看 ${escapeHtml(row.sku_code)} 出库明细" aria-expanded="${detailState?.open && detailState.skuCode === row.sku_code ? "true" : "false"}"${detailState?.phase === "loading" ? " disabled" : ""}>${content}</button>` : content;
        return `<td class="table-column-${column.key} table-align-${column.align} table-kind-${column.kind}">${cell}${unit ? `<em>${escapeHtml(unit)}</em>` : ""}</td>`;
      }).join("")}</tr>`).join("")
      : "";
    const tableKeys = new Set(table.columns.map((column) => column.key));
    const tableVariant = tableKeys.has("net_sales_quantity") ? " business-table-orders business-table-return-quantities" : tableKeys.has("manager_key") ? " business-table-managers" : tableKeys.has("outbound_code") ? " business-table-shipment-detail" : tableKeys.has("sales_order_code") ? " business-table-orders" : tableKeys.size === 2 && tableKeys.has("month") && tableKeys.has("order_count") ? " business-table-months" : tableKeys.has("name_status") ? " business-table-names" : tableKeys.has("received_quantity") ? " business-table-detail" : tableKeys.has("box_quantity") ? " business-table-boxes" : ["rank", "sku_name", "sku_code"].every((key) => tableKeys.has(key)) ? " business-table-ranking" : "";
    const emptyNotice = table.rows.length ? "" : `<p class="business-table-empty">${escapeHtml(table.emptyLabel)}</p>`;
    const notes = table.notes?.length ? `<ul class="business-result-notes">${table.notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>` : "";
    const exportActions = table.rows.length
      ? `<div class="table-export-actions" role="group" aria-label="导出查询结果"><span class="table-export-label">导出</span>${[
          ["excel", "Excel"], ["word", "Word"], ["csv", "CSV"], ["png", "PNG"], ["pdf", "PDF / 打印"],
        ].map(([format, label]) => `<button type="button" class="table-export-button" data-table-export="${format}" data-table-export-event="${eventIndex}" data-table-export-index="${tableIndex}" title="导出当前结果为${label}"><svg class="icon" aria-hidden="true"><use href="#icon-download" /></svg>${label}</button>`).join("")}${renderTableReportActions(eventIndex, tableIndex, table.rows.length > 0)}</div>`
      : "";
    return `<div class="business-table-block${tableVariant}">` +
      `<div class="business-result-heading"><div class="business-result-heading-copy"><div class="business-result-label">查询结果</div><h3 class="business-result-title">${escapeHtml(table.title)}</h3>${table.caption ? `<p class="business-result-caption">${escapeHtml(table.caption)}</p>` : ""}</div><span class="business-table-count">${escapeHtml(countLabel)}</span></div>` +
      `${summary}${exportActions}<div class="business-table-scroll" tabindex="0"><table class="business-table"><caption class="sr-only">${escapeHtml(table.title)}</caption><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div>${emptyNotice}` +
      `${renderResultPager(table.pagination)}${notes}${renderDetailBatchControls(table, eventIndex)}</div>`;
  }

  function latestDetailMetadata(meta) {
    return state.sessions.flatMap(session => session.events).reduce((latest, event) => {
      const candidate = event.businessTable?.detail_batches;
      return candidate?.result_id === meta.result_id && candidate.next_batch > latest.next_batch ? candidate : latest;
    }, meta);
  }

  function renderDetailBatchControls(table, eventIndex) {
    if (!table.detail_batches || eventIndex < 0) return "";
    const meta = latestDetailMetadata(table.detail_batches);
    const expired = meta.expires_at * 1000 <= Date.now();
    const invalid = state.sessions.some(session => session.events.some(event => event.detailBatch?.result_id === meta.result_id
      && ["detail_source_changed", "detail_continuation_unavailable"].includes(event.errorCode)));
    const unavailable = expired || invalid;
    const label = meta.complete ? "全部批次已取回" : unavailable ? "分批任务已失效，需重新查询" : "原查询范围保持不变";
    const downloadKey = `${table.detail_batches.result_id}:${table.detail_batches.next_batch}`;
    const exportId = table.detail_batches.export_id;
    const exportState = exportId && detailExporter?.status(exportId);
    const exportPanel = exportState ? `<div class="detail-export-controls" data-export-panel="${exportId}">` +
      `<div class="detail-batch-progress" role="status"><strong>${exportState.complete ? "完整文件已保存" : `文件已保存 ${formatTableCount(exportState.loaded)} / ${formatTableCount(meta.total_rows)} 行`}</strong>` +
      `<span>${escapeHtml(exportState.message || (exportState.loaded ? "导出未完成，可继续" : "全部明细导出"))}</span></div>` +
      `<progress max="${meta.total_rows}" value="${exportState.loaded}" aria-label="文件保存进度"></progress>` +
      `<button class="detail-batch-next" type="button" data-full-export="${exportId}"${!exportState.ready || !state.sessionReady ? " disabled" : ""}>` +
      `<svg class="icon" aria-hidden="true"><use href="#${exportState.running ? "icon-close" : "icon-download"}" /></svg>` +
      `${exportState.running ? "暂停导出" : exportState.complete ? "核验已保存文件" : exportState.loaded ? "继续导出全部" : "导出全部明细"}</button>` +
      `${!exportState.running && exportState.record && ["file_changed", "storage_failed"].includes(exportState.errorCode) ? `<button class="detail-batch-next" type="button" data-export-newfile="${exportId}"><svg class="icon" aria-hidden="true"><use href="#icon-download" /></svg>重新导出到新文件</button>` : ""}` +
      `${!exportState.running && ["detail_source_changed", "export_expired"].includes(exportState.errorCode) ? `<button class="detail-batch-next" type="button" data-export-requery="${eventIndex}"${state.loading ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#icon-retry" /></svg>重新查询原问题</button>` : ""}</div>` : "";
    return `<div class="detail-batch-controls"><div class="detail-batch-progress" role="status"><strong>${exportState ? "表格预览" : "已取回"} ${formatTableCount(meta.loaded_rows)} / ${formatTableCount(meta.total_rows)} 行</strong><span>${label}</span></div>` +
      `<div class="detail-batch-actions">${table.detail_batches.loaded_rows > 0 ? `<button class="icon-button" type="button" data-batch-download="${eventIndex}" title="下载本批完整明细" aria-label="下载本批完整明细"${batchDownloads.has(downloadKey) ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#icon-download" /></svg></button>` : ""}` +
      `${!meta.complete ? `<button class="detail-batch-next" type="button" data-detail-batch="${eventIndex}"${state.loading || !state.sessionReady ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#${unavailable ? "icon-retry" : "icon-chevron"}" /></svg>${unavailable ? "重新查询原问题" : `取回第${meta.next_batch}批`}</button>` : ""}</div></div>${exportPanel}`;
  }

  async function downloadDetailBatch(event) {
    const table = event?.businessTable;
    if (!table?.detail_batches || !table.detail_batches.loaded_rows) return;
    const key = `${table.detail_batches.result_id}:${table.detail_batches.next_batch}`;
    if (batchDownloads.has(key)) return;
    batchDownloads.add(key); renderMessages();
    const conversation = state.backendConversationId;
    try {
      const pages = table.pagination;
      const rows = [];
      for (let page = 1; page <= (pages?.page_count || 1); page += 1) {
        const part = !pages || page === pages.page ? table : normalizeBusinessTable(
          (await fetchJsonWithRetry(`/v1/results/${pages.result_id}/pages/${page}`, {}, HEALTH_TIMEOUT_MS, true)).business_table, "completed");
        if (conversation !== state.backendConversationId || !state.sessionReady || !part
          || !["title", "caption", "columns", "summary", "notes", "detail_batches"].every(field => JSON.stringify(part[field]) === JSON.stringify(table[field]))
          || (pages && (part.pagination?.result_id !== pages.result_id || part.pagination.page !== page
            || part.pagination.total_rows !== pages.total_rows || part.pagination.start_row !== rows.length + 1))) throw new ApiError("", "query_invalid_result", 502);
        rows.push(...part.rows);
        if (rows.length > 5000) throw new ApiError("", "query_invalid_result", 502);
      }
      if (rows.length !== (pages?.total_rows || table.rows.length)) throw new ApiError("", "query_invalid_result", 502);
      const cell = value => {
        let text = String(value ?? "");
        if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
        return '"' + text.replace(/"/g, '""') + '"';
      };
      const csv = [table.columns.map(column => cell(column.label + (column.unit ? `（${column.unit}）` : ""))).join(","),
        ...rows.map(row => table.columns.map(column => cell(row[column.key])).join(","))].join("\r\n");
      const url = URL.createObjectURL(new Blob(["\uFEFF", csv], {type:"text/csv;charset=utf-8"}));
      const link = document.createElement("a");
      link.href = url; link.download = `皇家小虎出库明细-第${table.detail_batches.next_batch - 1}批.csv`;
      link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast("本批完整明细已下载");
    } catch (error) { showToast(safeErrorMessage(error.code || "network_error", error.status)); }
    finally { batchDownloads.delete(key); renderMessages(); }
  }

  function renderResultPager(page) {
    if (!page) return "";
    const status = resultPageStates.get(page.result_id) || {};
    const expired = page.expires_at * 1000 <= Date.now();
    const disabled = status.loading || expired;
    const message = expired ? "结果已过期，请重新查询" : status.error || (status.loading ? "正在加载结果页" : "");
    return `<div class="result-pagination" data-result-id="${page.result_id}" aria-busy="${status.loading ? "true" : "false"}">` +
      `<span class="result-page-range">${page.start_row}-${page.end_row} / ${page.total_rows} 行</span>` +
      `<div class="result-page-controls"><button type="button" class="icon-button result-page-prev" data-result-page="${page.page - 1}" title="上一页" aria-label="上一页"${disabled || page.page === 1 ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#icon-chevron" /></svg></button>` +
      `<label class="result-page-label">第 <input type="number" aria-label="结果页码" min="1" max="${page.page_count}" value="${page.page}"${disabled ? " disabled" : ""} /> / ${page.page_count} 页</label>` +
      `<button type="button" class="icon-button result-page-next" data-result-page="${page.page + 1}" title="下一页" aria-label="下一页"${disabled || page.page === page.page_count ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#icon-chevron" /></svg></button></div>` +
      `<span class="result-page-status" role="status">${escapeHtml(message)}</span></div>`;
  }

  async function loadResultPage(resultId, pageNumber) {
    const session = activeSession();
    const event = session.events.find((event) => [...(event.businessTables || [event.businessTable]), event.drilldown?.table].some((table) => table?.pagination?.result_id === resultId));
    const selected = event?.drilldown?.table?.pagination?.result_id === resultId ? event.drilldown : null;
    const tables = selected ? [selected.table] : event?.businessTables || [event?.businessTable];
    const tableIndex = tables.findIndex((table) => table?.pagination?.result_id === resultId);
    const previousTable = tables[tableIndex];
    const current = previousTable?.pagination;
    if (!current || resultPageStates.get(resultId)?.loading || !Number.isSafeInteger(pageNumber) || pageNumber < 1 || pageNumber > current.page_count || pageNumber === current.page) return;
    const conversation = state.backendConversationId;
    resultPageStates.set(resultId, { loading: true });
    renderMessages();
    try {
      const payload = await fetchJson(`/v1/results/${resultId}/pages/${pageNumber}`, {}, HEALTH_TIMEOUT_MS);
      const table = normalizeBusinessTable(payload.business_table, "completed");
      const sameReport = table && ["title", "caption", "emptyLabel", "columns", "summary", "notes", "detail_batches"].every(
        (key) => JSON.stringify(table[key]) === JSON.stringify(previousTable[key])
      );
      if (!sameReport || table.pagination?.result_id !== resultId || table.pagination.page !== pageNumber || table.pagination.total_rows !== current.total_rows || table.pagination.page_count !== current.page_count || table.pagination.expires_at !== current.expires_at) throw new Error("结果页校验失败，请重新查询");
      if (selected && (!validDrilldownTable(table, event.businessTable, selected.skuCode) || JSON.stringify(table.summary) !== JSON.stringify(previousTable.summary))) throw new Error("明细页与原排名不一致，未替换已有数据");
      if (state.backendConversationId === conversation && state.sessions.includes(session) && session.events.includes(event)) {
        if (selected) {
          if (event.drilldown === selected) selected.table = table;
        } else {
          if (event.businessTables) event.businessTables[tableIndex] = table;
          if (!event.businessTables || tableIndex === 0) event.businessTable = table;
        }
        persistUiHistory();
      }
      resultPageStates.delete(resultId);
    } catch (error) {
      resultPageStates.set(resultId, { loading: false, error: error instanceof Error ? error.message : "结果页加载失败，请重试" });
    } finally {
      if (state.backendConversationId === conversation && state.sessions.includes(session) && activeSession() === session) {
        renderMessages();
        persistUiHistory();
        elements.messageList.querySelector(`[data-result-id="${resultId}"] input`)?.focus({ preventScroll: true });
      }
    }
  }

  function renderBusinessResult(businessResult, businessTable, businessTables, eventIndex = -1, detailState = null) {
    if (businessTables?.length) {
      return businessTables.map((table, tableIndex) => `<section class="business-result business-result-table" aria-label="${escapeHtml(table.title)}">${renderBusinessTable(table, eventIndex, detailState, tableIndex)}</section>`).join("");
    }
    if (businessTable) {
      return `<section class="business-result business-result-table" aria-label="查询结果">${renderBusinessTable(businessTable, eventIndex, detailState, 0)}</section>`;
    }
    if (!businessResult?.text) return "";
    return `<section class="business-result" aria-label="查询结果"><div class="business-result-label">查询结果</div>` +
      `<div class="business-result-text rich-text">${renderRichText(businessResult.text)}</div></section>`;
  }

  function renderFeedbackControls(event, index) {
    const requestId = boundedText(event?.requestId, 200);
    if (!requestId || event?.kind === "running") return "";
    if (event.feedback?.type) {
      const label = event.feedback.type === "accepted" ? "结果已标记为准确" : "问题已记录到学习总结";
      return `<div class="feedback-recorded"><svg class="icon" aria-hidden="true"><use href="#icon-check" /></svg><span>${label}</span></div>`;
    }
    const disabled = state.queryFeedbackSubmitting ? " disabled" : "";
    if (event.outcome === "completed") {
      return `<div class="answer-feedback" aria-label="结果反馈">` +
        `<span class="answer-feedback-label">结果是否准确？</span>` +
        `<button class="feedback-action" type="button" data-feedback-action="accepted" data-feedback-index="${index}"${disabled}><svg class="icon" aria-hidden="true"><use href="#icon-thumbs-up" /></svg>结果准确</button>` +
        `<button class="feedback-action feedback-action-muted" type="button" data-feedback-action="correction" data-feedback-index="${index}"${disabled}><svg class="icon" aria-hidden="true"><use href="#icon-message-square" /></svg>需要优化</button>` +
        `</div>`;
    }
    return `<div class="answer-feedback" aria-label="执行反馈">` +
      `<button class="feedback-action feedback-action-muted" type="button" data-feedback-action="correction" data-feedback-index="${index}"${disabled}><svg class="icon" aria-hidden="true"><use href="#icon-message-square" /></svg>报告问题</button>` +
      `</div>`;
  }

  function renderExecution(event) {
    const live = event.kind === "running";
    const label = live ? (event.stageLabel || "正在处理请求") : resultLabel(event.outcome, event.resultState, event.guidance, event.queryStatus);
    if (event.resultState === "explanation" && event.guidance) {
      const guidanceLabel = event.guidance.kind === "sql_conversion" ? {
        needs_input: "待补充转换材料，未查询数据", converted: "转换检查完成，未查询数据", rejected: "转换未通过，未查询数据"
      }[event.guidance.state] : label;
      return `<article class="message assistant guidance-message" role="status">` +
        `<div class="message-avatar" aria-hidden="true">${assistantAvatarMarkup()}</div><div class="message-body">` +
        `<div class="message-meta">皇家小虎取数运行时 · ${escapeHtml(guidanceLabel)}</div>${renderRequestReference(event.question)}` +
        `<section class="business-result" aria-label="${escapeHtml(event.guidance.title)}"><div class="business-result-label">${escapeHtml(event.guidance.title)}</div>` +
        `<div class="business-result-text rich-text">${renderRichText(event.guidance.text)}</div></section>${contextMarkup(event.context)}</div></article>`;
    }
    const visualState = live ? "running" : outcomeKey(event.outcome);
    const icon = statusIcon(visualState);
    const bodyClass = event.businessTable ? "message-body message-body-table" : "message-body";
    const stopped = event.outcome === "failed" || event.outcome === "blocked";
    const message = event.resultState === "presentation_error" ? PRESENTATION_ERROR_MESSAGE
      : event.statusMessage || (stopped ? "本次未形成可确认的实时数据结果，请重新提交" : "");
    const statusMessage = message
      ? `<div class="execution-status-message" role="note">${escapeHtml(message)}</div>`
      : "";
    return `<article class="message assistant execution-message" data-execution-state="${visualState}" role="status">` +
      `<div class="message-avatar" aria-hidden="true">${assistantAvatarMarkup()}</div>` +
      `<div class="${bodyClass}"><div class="message-meta">皇家小虎取数运行时 · 执行状态</div>` +
      `${renderRequestReference(event.question)}` +
      `${renderBusinessResult(event.businessResult, event.businessTable, event.businessTables, event.index, event.drilldown)}${renderReport(event, event.index)}${renderDrilldown(event)}` +
      `<div class="execution-card"><div class="execution-card-head"><span class="execution-icon"><svg class="icon" aria-hidden="true"><use href="#${icon}" /></svg></span><strong>${escapeHtml(label)}</strong></div>` +
      `${statusMessage}${renderStageList(event.process || normalizeProcess(null, event.outcome))}${renderArtifacts(event.artifacts)}${contextMarkup(event.context)}</div>${live ? renderCancelControl(event, event.index ?? -1) : stopped ? renderRetryControl(event, event.index ?? -1) : ""}${renderFeedbackControls(event, event.index ?? -1)}</div></article>`;
  }

  function renderRequestReference(question) {
    const value = displayQuestionText(question);
    if (!value) return "";
    return `<div class="request-reference"><span>本次问题</span><p>${escapeHtml(value)}</p></div>`;
  }

  function renderError(event, index) {
    const question = displayQuestionText(event?.question);
    const stateLabel = event.errorCode === "business_definition_unpublished" ? "口径待确认 · 未执行查询" : "未完成";
    const questionMarkup = question
      ? `<div class="error-question"><span>本次问题</span><p>${escapeHtml(question)}</p></div>`
      : "";
    return `<article class="message assistant" role="alert"><div class="message-avatar" aria-hidden="true">${assistantAvatarMarkup()}</div><div class="message-body"><div class="message-meta">皇家小虎取数运行时 · ${stateLabel}</div>${questionMarkup}<div class="message-error">${escapeHtml(event.text || "本次请求未完成，请稍后重试")}</div>${renderRetryControl(event, index)}${renderFeedbackControls({...event, outcome: "failed"}, index)}</div></article>`;
  }

  function renderRetryControl(event, index) {
    if (["presentation_error", "clarification_error"].includes(event?.resultState)) return "";
    if (event?.errorCode === "business_definition_unpublished" || event?.resultState === "business_definition_unpublished") return "";
    if (!displayQuestionText(event?.question) || index < 0) return "";
    return `<button class="retry-button" type="button" data-retry-index="${index}"${state.loading ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="#icon-retry" /></svg>重试本次问题</button>`;
  }

  function renderCancelControl(event, index) {
    if (event?.kind !== "running" || !event.taskId || index < 0) return "";
    const disabled = event.canceling ? " disabled" : "";
    const label = event.canceling ? "正在停止查询" : "停止查询";
    return `<button class="cancel-query-button" type="button" data-cancel-index="${index}"${disabled} aria-label="${label}"><svg class="icon" aria-hidden="true"><use href="#icon-close" /></svg>${label}</button>`;
  }

  function renderMessages() {
    const session = activeSession();
    const hasEvents = session.events.length > 0;
    elements.welcomeState.hidden = hasEvents;
    elements.chatScroll.setAttribute("aria-busy", state.loading ? "true" : "false");
    elements.messageList.innerHTML = session.events.map((event, index) => {
      if (event.kind === "question") return renderUserQuestion(event);
      if (event.kind === "proposal") return proposalReview.render(event, index);
      if (event.kind === "interpretation") return interpretation.render(event, index);
      if (event.kind === "clarification") return renderClarification(event, index);
      if (event.kind === "error") return renderError(event, index);
      return renderExecution({...event, index});
    }).join("");
    proposalReview.bind(elements.messageList, session);
    interpretation.bind(elements.messageList, session);
    elements.messageList.querySelectorAll("[data-drilldown-sku]").forEach((button) => {
      button.addEventListener("click", () => openDrilldown(session, session.events[Number(button.dataset.drilldownIndex)], button.dataset.drilldownSku));
    });
    elements.messageList.querySelectorAll("[data-detail-batch]").forEach(button => {
      button.addEventListener("click", () => {
        if (state.loading) return;
        const event = session.events[Number(button.dataset.detailBatch)];
        if (!event?.businessTable?.detail_batches) return;
        const meta = latestDetailMetadata(event.businessTable.detail_batches);
        const invalid = state.sessions.some(s => s.events.some(e => e.detailBatch?.result_id === meta.result_id
          && ["detail_source_changed", "detail_continuation_unavailable"].includes(e.errorCode)));
        void sendQuestion(event.question, null, "", {session, reuseQuestion:true,
          detailBatch: meta.expires_at * 1000 <= Date.now() || invalid ? null : {result_id: meta.result_id, batch_index: meta.next_batch}});
      });
    });
    elements.messageList.querySelectorAll("[data-full-export]").forEach(button => {
      const id = button.dataset.fullExport;
      detailExporter.hydrate(id);
      button.addEventListener("click", () => detailExporter.status(id).running ? detailExporter.pause(id) : void detailExporter.start(id));
    });
    elements.messageList.querySelectorAll("[data-export-newfile]").forEach(button => {
      button.addEventListener("click", () => void detailExporter.start(button.dataset.exportNewfile, true));
    });
    elements.messageList.querySelectorAll("[data-export-requery]").forEach(button => {
      button.addEventListener("click", () => {
        const event = session.events[Number(button.dataset.exportRequery)];
        if (!state.loading && event?.question) void sendQuestion(event.question, null, "", {session, reuseQuestion:true});
      });
    });
    elements.messageList.querySelectorAll("[data-batch-download]").forEach(button => {
      button.addEventListener("click", () => downloadDetailBatch(session.events[Number(button.dataset.batchDownload)]));
    });
    elements.messageList.querySelectorAll("[data-table-export]").forEach(button => {
      button.addEventListener("click", () => {
        if (!tableExporter || button.disabled) return;
        const eventIndex = Number(button.dataset.tableExportEvent);
        const tableIndex = Number(button.dataset.tableExportIndex);
        const event = session.events[eventIndex];
        const table = tableIndex === -1
          ? event?.drilldown?.table
          : event?.businessTables?.[tableIndex] || (tableIndex === 0 ? event?.businessTable : null);
        if (!table) {
          showToast("结果表已不可用，请重新查询");
          return;
        }
        button.disabled = true;
        void tableExporter.exportTable(table, button.dataset.tableExport).finally(() => {
          button.disabled = false;
        });
      });
    });
    elements.messageList.querySelectorAll("[data-generate-report]").forEach(button => {
      button.addEventListener("click", () => {
        const index = Number(button.dataset.generateReport);
        const event = session.events[index];
        if (!event || state.loading) return;
        const instructions = elements.messageList.querySelector(`[data-report-instructions="${index}"]`)?.value || "";
        void generateReport(session, event, index, instructions);
      });
    });
    elements.messageList.querySelectorAll("[data-drilldown-close]").forEach((button) => {
      button.addEventListener("click", () => {
        const index = Number(button.dataset.drilldownClose);
        const detail = session.events[index]?.drilldown;
        if (!detail) return;
        detail.open = false;
        persistUiHistory(); renderMessages();
        [...elements.messageList.querySelectorAll(`[data-drilldown-index="${index}"]`)].find((item) => item.dataset.drilldownSku === detail.skuCode)?.focus({ preventScroll: true });
      });
    });
    elements.messageList.querySelectorAll("[data-drilldown-retry]").forEach((button) => {
      button.addEventListener("click", () => {
        const event = session.events[Number(button.dataset.drilldownRetry)];
        if (event?.drilldown) openDrilldown(session, event, event.drilldown.skuCode, true);
      });
    });
    elements.messageList.querySelectorAll("[data-result-id]").forEach((pager) => {
      pager.querySelectorAll("[data-result-page]").forEach((button) => button.addEventListener("click", () => loadResultPage(pager.dataset.resultId, Number(button.dataset.resultPage))));
      pager.querySelector("input")?.addEventListener("change", (event) => {
        if (event.target.reportValidity()) loadResultPage(pager.dataset.resultId, Number(event.target.value));
      });
    });
    elements.messageList.querySelectorAll("[data-clarification-submit]").forEach((button) => {
      button.addEventListener("click", () => submitClarification(Number(button.dataset.clarificationSubmit)));
    });
    elements.messageList.querySelectorAll("[data-clarification-index]").forEach((card) => {
      const saveChoices = () => {
        const event = session.events[Number(card.dataset.clarificationIndex)];
        if (!event || event.submitting || state.loading) return;
        const error = card.querySelector(".clarification-error");
        if (error) error.hidden = true;
        card.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
        card.querySelectorAll("input[type=date]").forEach((input) => input.setCustomValidity(""));
        const selected = readClarificationChoices(card, event.clarification);
        card.querySelectorAll("[data-date-range]").forEach((fields) => {
          const active = answerHasOption(selected, fields.dataset.dateRange, DATE_RANGE_OPTIONS[fields.dataset.dateRange]);
          fields.hidden = !active;
          fields.querySelectorAll("input").forEach((input) => { input.disabled = !active; });
        });
        event.selectedAnswers = selected;
        persistUiHistory();
      };
      card.addEventListener("change", saveChoices);
      card.addEventListener("input", saveChoices);
    });
    elements.messageList.querySelectorAll("[data-retry-index]").forEach((button) => {
      button.addEventListener("click", () => {
        if (state.loading) return;
        const event = session.events[Number(button.dataset.retryIndex)];
        if (event) {
          if (event.question) {
            // A transport interruption may have reached the server, so reuse
            // its receipt. A server-side 5xx is already a definitive failed
            // receipt and must get a fresh key to actually run again.
            const replayRequest = REPLAYABLE_ERROR_CODES.has(event.errorCode);
            if (event.conversionSubmitted && (!replayRequest || !event.taskId)) {
              showToast("原始转换材料未保留，请重新提交材料");
              elements.conversionFile.click();
              return;
            }
              void sendQuestion(event.question, null, "", {
                session,
                reuseQuestion: true,
                ...(normalizeRuleSubmission(event) || {}),
                // A transport failure may have reached the server. Replaying the
              // same receipt recovers it without creating a second model turn.
              requestKey: replayRequest ? event.requestKey : "",
              taskId: replayRequest ? event.taskId : "",
              detailBatch: ["detail_source_changed", "detail_continuation_unavailable"].includes(event.errorCode) ? null : event.detailBatch,
              conversionSubmitted: event.conversionSubmitted === true
            });
          } else {
            showToast("请重新输入取数问题");
          }
        }
      });
    });
    elements.messageList.querySelectorAll("[data-cancel-index]").forEach((button) => {
      button.addEventListener("click", () => {
        const index = Number(button.dataset.cancelIndex);
        const event = session.events[index];
        if (!event || event.kind !== "running" || !event.taskId || event.canceling) return;
        event.canceling = true;
        updateComposerState();
        renderMessages();
        void cancelTask(event.taskId).catch((error) => {
          if (!session.events.includes(event)) return;
          event.canceling = false;
          updateComposerState();
          renderMessages();
          showToast(error instanceof Error ? error.message : "停止查询失败，请稍后重试");
        });
      });
    });
    elements.messageList.querySelectorAll("[data-feedback-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const index = Number(button.dataset.feedbackIndex);
        const type = button.dataset.feedbackAction;
        const event = session.events[index];
        if (!event || !event.requestId || state.queryFeedbackSubmitting) return;
        if (type === "accepted") submitQueryFeedback(session, event, "accepted");
        if (type === "correction") openQueryFeedback(session, event, index);
      });
    });
  }

  function render() {
    const session = activeSession();
    elements.breadcrumbTitle.textContent = session.title;
    renderSidebar();
    renderMessages();
    updateComposerState();
    persistUiHistory();
    window.requestAnimationFrame(() => { elements.chatScroll.scrollTop = elements.chatScroll.scrollHeight; });
  }

  function resetInspector() {
    state.inspectorSnapshot = null;
    elements.inspectorEmpty.hidden = false;
    elements.inspectorContent.hidden = true;
    elements.traceStatusText.textContent = "等待执行";
    elements.traceContext.textContent = "-";
    elements.traceCompression.textContent = "-";
    elements.traceElapsed.textContent = "-";
    elements.traceOutcome.textContent = "-";
    elements.traceProcess.innerHTML = "";
    updateContextStatus(state.contextState);
  }

  function updateContextStatus(rawContext) {
    const context = rawContext || normalizeContext(null);
    state.contextState = context;
    const inherited = context.inherited;
    elements.contextStatus.innerHTML = `<span class="status-dot ${inherited ? "online" : ""}"></span><span>${escapeHtml(inherited ? "上下文已继承" : "上下文已绑定")}</span>`;
  }

  function updateInspector(snapshot, startedAt) {
    const context = snapshot.context || normalizeContext(null);
    state.inspectorSnapshot = snapshot;
    elements.inspectorEmpty.hidden = true;
    elements.inspectorContent.hidden = false;
    elements.inspectorContent.dataset.executionState = outcomeKey(snapshot.outcome);
    elements.inspectorContent.querySelector(".trace-check use")?.setAttribute("href", `#${statusIcon(outcomeKey(snapshot.outcome))}`);
    elements.traceStatusText.textContent = snapshot.outcome === "running" ? "正在执行" : resultLabel(snapshot.outcome, snapshot.resultState, snapshot.guidance, snapshot.queryStatus);
    elements.traceContext.textContent = context.contextLabel;
    elements.traceCompression.textContent = context.compressionLabel;
    elements.traceElapsed.textContent = startedAt ? `${Math.max(0, Math.round(performance.now() - startedAt))} ms` : "-";
    elements.traceOutcome.textContent = snapshot.outcomeLabel;
    elements.traceProcess.innerHTML = (snapshot.process || []).map((step) => `<li class="stage-${escapeHtml(step.state)}"><span class="stage-marker"><svg class="icon" aria-hidden="true"><use href="#${statusIcon(step.state)}" /></svg></span><span>${escapeHtml(stageLabel(step))}</span></li>`).join("");
    updateContextStatus(context);
  }

  function restoreInspectorFromSession() {
    const session = activeSession();
    const latest = [...session.events].reverse().find((event) => (
      event?.kind === "proposal" || event?.kind === "interpretation" ||
      event?.kind === "execution" ||
      event?.kind === "error" ||
      event?.kind === "running"
    ));
    if (!latest) {
      updateContextStatus(session.contextState || state.contextState);
      return;
    }
    if (latest.kind === "proposal" || latest.kind === "interpretation") {
      resetInspector();
      return;
    }
    if (latest.kind === "error") {
      const unpublished = latest.errorCode === "business_definition_unpublished";
      const errorOutcome = unpublished ? "blocked" : "failed";
      updateInspector({
        outcome: errorOutcome,
        resultState: latest.errorCode,
        outcomeLabel: resultLabel(errorOutcome, latest.errorCode),
        context: latest.context || session.contextState || state.contextState || normalizeContext(null),
        process: normalizeProcess(null, errorOutcome)
      }, null);
      return;
    }
    const outcome = outcomeKey(latest.outcome || latest.process?.state || "completed");
    updateInspector({
      outcome,
      resultState: latest.resultState,
      queryStatus: latest.queryStatus,
      outcomeLabel: resultLabel(outcome, latest.resultState, latest.guidance, latest.queryStatus),
      guidance: latest.guidance,
      context: latest.context || session.contextState || state.contextState || normalizeContext(null),
      process: latest.process || normalizeProcess(null, outcome)
    }, null);
  }

  function detectReportKind(question) {
    const normalized = String(question || "").replace(/\s+/g, "");
    if (!normalized) return null;
    const asksForImage = normalized.includes("图片") || normalized.includes("报表图") || normalized.endsWith("周转图") || normalized.includes("生成图");
    return asksForImage && normalized.includes("中心仓") && normalized.includes("分仓") && normalized.includes("周转") && /(?:\d{1,4}|[一二三四五六七八九十]{1,3})(?:个完整自然日|日|天)/.test(normalized) && !/月|去年|在途|扣减|渠道|排除/.test(normalized) ? REPORT_KIND : null;
  }

  function profileFromAnswers(answers) {
    if (!Array.isArray(answers)) return null;
    for (const answer of answers) {
      const values = Array.isArray(answer?.option_ids)
        ? answer.option_ids
        : answer?.option_id ? [answer.option_id] : [];
      if (values.includes("available_plus_transit_finished_goods")) return "available_plus_transit_finished_goods";
      if (values.includes("actual_finished_goods")) return "actual_finished_goods";
      if (values.includes("record_deduction_all")) return "record_deduction_all";
    }
    return null;
  }

  function optionIdsFromAnswer(answer) {
    const rawOptions = Array.isArray(answer?.option_ids)
      ? answer.option_ids
      : answer?.option_id ? [answer.option_id] : [];
    return [...new Set(rawOptions.map((value) => String(value || "").trim()).filter(Boolean))];
  }

  function answerHasOption(answers, questionId, optionId) {
    if (!Array.isArray(answers)) return false;
    return answers.some((answer) => (
      String(answer?.question_id || "").trim() === questionId &&
      optionIdsFromAnswer(answer).includes(optionId)
    ));
  }

  function cloneClarificationAnswers(answers) {
    if (!Array.isArray(answers)) return null;
    return answers.map((answer) => ({
      question_id: String(answer?.question_id || "").trim(),
      option_ids: optionIdsFromAnswer(answer),
      ...(storedDateRange(answer?.date_range) ? {date_range: storedDateRange(answer.date_range)} : {})
    }));
  }

  function clarificationAcceptsAnswers(rawClarification, answers) {
    const clarification = cloneClarification(rawClarification);
    if (!clarification || !Array.isArray(answers) || answers.length !== clarification.questions.length) return false;
    const answersByQuestion = new Map();
    for (const answer of answers) {
      const questionId = String(answer?.question_id || "").trim();
      if (!questionId || answersByQuestion.has(questionId)) return false;
      answersByQuestion.set(questionId, optionIdsFromAnswer(answer));
    }
    return clarification.questions.every((question) => {
      const selected = answersByQuestion.get(question.id) || [];
      const allowed = new Set(question.options.map((option) => option.id));
      const range = answers.find((answer) => answer.question_id === question.id)?.date_range;
      const needsRange = selected.includes(DATE_RANGE_OPTIONS[question.id]);
      return (!needsRange || validDateRange(range)) && (!range || needsRange) && selected.length > 0 &&
        (question.multiple || selected.length === 1) &&
        selected.every((optionId) => allowed.has(optionId));
    });
  }

  function validDateRange(value) {
    const range = storedDateRange(value);
    const validDate = (text) => text && Number.isFinite(Date.parse(`${text}T00:00:00Z`)) &&
      new Date(`${text}T00:00:00Z`).toISOString().slice(0, 10) === text;
    return range && validDate(range.start) && validDate(range.end) && range.start < range.end;
  }

  let toastTimer;
  function showToast(message) {
    elements.toast.textContent = message;
    elements.toast.classList.add("visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 2800);
  }

  function openQueryFeedback(session, event, index) {
    if (!session || !event?.requestId || state.queryFeedbackSubmitting) return;
    state.feedbackTarget = {
      sessionId: session.id,
      event,
      index,
      requestId: boundedText(event.requestId, 200)
    };
    if (typeof elements.queryFeedbackForm.reset === "function") elements.queryFeedbackForm.reset();
    if (typeof elements.queryFeedbackDialog.showModal === "function") elements.queryFeedbackDialog.showModal();
    else elements.queryFeedbackDialog.setAttribute("open", "open");
    window.requestAnimationFrame(() => {
      const first = elements.queryFeedbackForm.querySelector("input[name='query_feedback_issue']");
      if (first) first.focus();
    });
  }

  function closeQueryFeedback() {
    if (typeof elements.queryFeedbackDialog.close === "function" && elements.queryFeedbackDialog.open) elements.queryFeedbackDialog.close();
    else elements.queryFeedbackDialog.removeAttribute("open");
    state.feedbackTarget = null;
    if (elements.queryFeedbackNote) elements.queryFeedbackNote.value = "";
  }

  async function submitQueryFeedback(session, event, feedbackType, issueType = null, note = null) {
    if (!session || !event?.requestId || state.queryFeedbackSubmitting || !state.sessionReady) return;
    state.queryFeedbackSubmitting = true;
    render();
    try {
      await fetchJson(QUERY_FEEDBACK_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrfToken },
        body: JSON.stringify({
          conversation_id: state.backendConversationId,
          request_id: boundedText(event.requestId, 200),
          feedback_type: feedbackType,
          issue_type: issueType,
          note: note || null
        })
      }, HEALTH_TIMEOUT_MS);
      event.feedback = { type: feedbackType, issueType: issueType || null };
      closeQueryFeedback();
      showToast(feedbackType === "accepted" ? "已记录为准确结果" : "已记录到学习总结");
      await refreshLearningSummary(false);
    } catch (error) {
      if (error?.code === "session_security_error") invalidateSession(error.message);
      showToast(error instanceof Error ? error.message : "反馈未完成，请稍后重试");
    } finally {
      state.queryFeedbackSubmitting = false;
      render();
    }
  }

  function submitQueryFeedbackForm(event) {
    event.preventDefault();
    const target = state.feedbackTarget;
    if (!target || !target.event) return;
    if (!elements.queryFeedbackForm.reportValidity()) return;
    const selected = elements.queryFeedbackForm.querySelector("input[name='query_feedback_issue']:checked");
    if (!selected) return;
    const session = state.sessions.find((item) => item.id === target.sessionId);
    if (!session) {
      closeQueryFeedback();
      return;
    }
    submitQueryFeedback(
      session,
      target.event,
      "correction",
      selected.value,
      elements.queryFeedbackNote.value.trim()
    );
  }

  function setLoading(value) {
    state.loading = value;
    updateComposerState();
    renderSidebar();
  }

  class ApiError extends Error {
    constructor(message, code = "request_failed", status = 0, requestId = "") {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.status = status;
      this.requestId = boundedText(requestId, 200);
    }
  }

  function identityBindingLabel() {
    if (state.identityMode === "installation") return "本机实例已绑定";
    if (state.identityMode === "wecom_signed") return "企微身份已绑定";
    return "本机临时会话已绑定";
  }

  function safeErrorMessage(code, status) {
    const messages = {
      session_security_error: "会话校验失败，请刷新页面重试",
      conversation_not_found: "会话不存在或已过期，请新建界面会话",
      conversation_busy: "上一条请求仍在执行，请稍后重试",
      clarification_pending: "请先完成当前口径选择",
      clarification_invalid: "本次口径选择凭证已失效",
      session_limit_reached: "本机实例的会话数量已达上限",
      metric_feedback_not_available: "口径收集服务尚未开放",
      endpoint_not_found: "口径收集服务尚未开放",
      report_contract_not_released: "报表口径尚未审批发布，暂未生成图片",
      report_quality_blocked: "数据质量校验未通过，暂未生成图片",
      report_query_failed: "报表查询未完成，暂未生成图片",
      report_parameters_invalid: "报表口径参数无效，请重新选择",
      report_render_failed: "数据图片生成失败，请稍后重试",
      report_configuration_error: "报表口径尚未完成配置",
      learning_feedback_invalid: "反馈格式无效，请重新选择",
      learning_feedback_not_found: "该结果已无法关联，请重新提交问题",
      learning_error: "学习记录暂时不可用，查询不受影响",
      unapproved_contract_result: "当前返回涉及未审批业务口径，未展示结果",
      business_definition_unpublished: "分配数量与实际出库数量的单位关系尚未确认，差异口径暂未发布，本次未执行查询。实际出库量可单独查询。",
      executor_unavailable: "Doris 数据源尚未就绪，请检查连接配置",
      query_execution_error: "Doris 数据源连接失败，请检查连接配置",
      chat_execution_error: "取数模型执行失败，请稍后重试",
      service_unavailable: "取数服务暂时不可用，请稍后重试",
      query_plan_invalid: "查询计划未形成可执行查询，本次未连接数仓；请重新提交",
      execution_invalid: "查询结果校验未通过，未展示数据",
      execution_missing: "Doris 查询已完成，但 Agent 未解析到结果数据",
      row_count_mismatch: "查询结果行数校验不一致，未展示数据",
      query_cancelled: "查询已停止",
      chat_timeout: "本次取数等待超时，未返回可确认结果",
      semantic_query_error: "业务来源数据未通过完整性校验，暂未展示结果；请检查订单或退货来源的标识与关联完整性",
      query_rejected: "查询被后端限制拒绝，未执行取数",
      query_failed: "查询执行失败，未返回可确认结果",
      query_unavailable: "取数服务连接失败，请稍后重试",
      query_timeout: "查询执行超时，未返回完整结果",
      query_truncated: "查询结果已截断，不能作为完整结果；请分段查询并对账",
      query_invalid_result: "查询结果校验未通过，未展示数据",
      request_timeout: "本次请求仍在处理，请稍后重试",
      request_in_progress: "本次请求仍在处理，请稍后重试",
      idempotency_conflict: "请求已发生变化，请重新提交",
      invalid_response: "服务返回格式无效，请稍后重试",
      result_page_unavailable: status === 503 ? "结果缓存容量不足，请稍后重新查询" : status === 400 ? "结果页码无效" : "结果已过期或失效，请重新查询",
      detail_source_changed: "查询期间数据或口径已变化，请重新查询原问题；旧批次未继续拼接",
      detail_continuation_unavailable: "分批任务已过期或失效，请重新查询原问题",
      ranking_unavailable: status === 503 ? "排名缓存容量不足，请稍后重新查询排名" : "排名已过期或失效，请重新查询排名；已有结果仍保留"
    };
    return messages[code] || (status === 404 ? "服务入口尚未开放" : "请求未完成，请稍后重试");
  }

  function isTransientRequestError(error, includeHttpStatuses = false) {
    return Boolean(
      error && (
        error.code === "network_error" ||
        error.code === "request_timeout" ||
        error.code === "conversation_busy" ||
        (includeHttpStatuses && TRANSIENT_HTTP_STATUSES.has(Number(error.status)))
      )
    );
  }

  function transientRetryDelay(attempt, errorCode = "") {
    if (errorCode === "conversation_busy") return REQUEST_POLL_DELAY_MS;
    return Math.min(5000, TRANSIENT_REQUEST_RETRY_BASE_MS * (2 ** attempt));
  }

  async function fetchJsonWithRetry(
    url,
    options = {},
    timeoutMs = HEALTH_TIMEOUT_MS,
    includeHttpStatuses = false
  ) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await fetchJson(url, options, timeoutMs);
      } catch (error) {
        const retryLimit = error?.code === "conversation_busy"
          ? BUSY_REQUEST_RETRY_LIMIT
          : error?.code === "request_timeout"
          ? 1
          : TRANSIENT_REQUEST_RETRY_LIMIT;
        if (!isTransientRequestError(error, includeHttpStatuses) || attempt >= retryLimit) {
          throw error;
        }
        await new Promise((resolve) => window.setTimeout(resolve, transientRetryDelay(attempt, error?.code)));
      }
    }
  }

  async function fetchJson(url, options = {}, timeoutMs = HEALTH_TIMEOUT_MS) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        signal: controller.signal
      });
      let payload = null;
      try { payload = await response.json(); } catch (_error) { payload = null; }
      if (!response.ok) {
      const code = typeof payload?.error?.normalized_result_state === "string"
        ? payload.error.normalized_result_state
        : typeof payload?.error?.normalized_state === "string"
          ? payload.error.normalized_state
          : typeof payload?.error?.code === "string" ? payload.error.code : "request_failed";
      const safeMessage = typeof payload?.error?.message === "string"
        && payload.error.message.length <= 240
        && !BUSINESS_RESULT_UNSAFE_PATTERN.test(payload.error.message)
        ? payload.error.message
        : safeErrorMessage(code, response.status);
      throw new ApiError(
          safeMessage,
          code,
          response.status,
          response.headers.get("X-Request-ID") || payload?.request_id || ""
        );
      }
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new ApiError(
          safeErrorMessage("invalid_response", response.status),
          "invalid_response",
          response.status,
          response.headers.get("X-Request-ID") || payload?.request_id || ""
        );
      }
      return payload;
    } catch (error) {
      if (error?.name === "AbortError") throw new ApiError(safeErrorMessage("request_timeout", 504), "request_timeout", 504);
      if (error instanceof ApiError) throw error;
      throw new ApiError("网络连接失败，请检查聊天服务", "network_error", 0);
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  function validSessionPayload(payload) {
    return Boolean(
      (payload?.conversation_id === null || typeof payload?.conversation_id === "string") &&
      typeof payload?.csrf_token === "string" &&
      payload.csrf_token.length > 20
    );
  }

  async function allocateBackendConversation() {
    if (!state.sessionReady || !state.csrfToken) throw new ApiError("会话尚未就绪", "session_not_ready");
    const payload = await fetchJson("/v1/conversations", {
      method: "POST",
      headers: { "X-CSRF-Token": state.csrfToken, Accept: "application/json" }
    });
    if (typeof payload.conversation_id !== "string" || payload.conversation_id.length < 10) {
      throw new ApiError("会话初始化失败，请刷新页面重试", "invalid_conversation");
    }
    return payload.conversation_id;
  }

  async function resumePendingClarification() {
    if (!state.sessionReady || !state.csrfToken || !state.backendConversationId) {
      throw new ApiError("会话尚未就绪", "session_not_ready");
    }
    const payload = await fetchJson("/v1/clarification/resume", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
        Accept: "application/json"
      },
      body: JSON.stringify({ conversation_id: state.backendConversationId })
    }, HEALTH_TIMEOUT_MS);
    // The backend owns principal/conversation binding. Older compatible
    // responses may omit the id; a supplied mismatching id is always rejected.
    const checkedPayload = typeof payload.conversation_id === "string"
      ? payload
      : { ...payload, conversation_id: state.backendConversationId };
    ensureSharedConversation(checkedPayload);
    const clarification = cloneClarification(clarificationFromPayload(payload));
    return {
      clarification,
      context: payload.context_state ? normalizeContext(payload.context_state) : state.contextState || normalizeContext(null)
    };
  }

  async function waitForRequestReceipt() {
    await new Promise((resolve) => window.setTimeout(resolve, REQUEST_POLL_DELAY_MS));
  }

  async function cancelTask(taskId) {
    if (!/^task_[a-f0-9]{32}$/.test(taskId || "")) {
      throw new ApiError("任务编号无效", "invalid_response", 502);
    }
    const task = await fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
      headers: {
        "X-CSRF-Token": state.csrfToken,
        Accept: "application/json"
      }
    }, HEALTH_TIMEOUT_MS);
    if (!["queued", "running", "completed", "failed", "interrupted", "cancelled"].includes(task?.state)) {
      throw new ApiError("停止查询响应无效", "invalid_response", 502);
    }
    return task;
  }

  async function postChatRequest(body, requestKey, resumeTaskId = "", onTask = null) {
    let task = resumeTaskId
      ? await fetchJsonWithRetry(`/v1/tasks/${encodeURIComponent(resumeTaskId)}`, {}, HEALTH_TIMEOUT_MS, true)
      : await fetchJsonWithRetry("/v1/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
        "Idempotency-Key": requestKey
      },
      body: JSON.stringify(body)
    }, HEALTH_TIMEOUT_MS, true);
    const runningEvent = state.sessions.flatMap((session) => session.events)
      .find((event) => event.kind === "running" && event.requestKey === requestKey);
    const updateTask = async () => {
      if (onTask) {
        if (onTask(task) === false) throw new ApiError("", "request_obsolete", 409);
        return;
      }
      if (!runningEvent) return;
      runningEvent.taskId = task.task_id;
      const stages = {
        queued: ["received", "等待前一条请求完成"],
        routing: ["routing", "正在识别本次业务条件"],
        model: ["routing", "正在分析取数需求"],
        query: ["executing", "正在查询 Doris"],
        validation: ["finalizing", "正在校验并整理结果"],
        rendering: ["finalizing", "正在生成数据图片"]
      };
      const [id, label] = stages[task.stage] || ["routing", "正在处理请求"];
      runningEvent.stageLabel = runningEvent.conversionSubmitted ? "正在检查转换材料" : label;
      runningEvent.process = runningEvent.conversionSubmitted ? [] : normalizeProcess({steps: [{id, state: "running"}]}, "running");
      render();
      updateInspector(runningEvent, runningEvent.startedAt);
      if (runningEvent.cancelRequested && !runningEvent.cancelSent && ["queued", "running"].includes(task?.state)) {
        runningEvent.cancelSent = true;
        try {
          const cancelled = await cancelTask(task.task_id);
          if (cancelled?.state === "cancelled") task = cancelled;
        } catch (_error) {
          runningEvent.cancelRequested = false;
          runningEvent.cancelSent = false;
          runningEvent.canceling = false;
          render();
        }
      }
    };
    await updateTask();
    let polls = 0;
    while (["queued", "running"].includes(task?.state) && polls < REQUEST_POLL_LIMIT) {
      polls += 1;
      await waitForRequestReceipt();
      task = await fetchJsonWithRetry(`/v1/tasks/${encodeURIComponent(task.task_id)}`, {}, HEALTH_TIMEOUT_MS, true);
      await updateTask();
    }
    if (["queued", "running"].includes(task?.state)) {
      throw new ApiError(safeErrorMessage("request_in_progress", 202), "request_in_progress", 202);
    }
    if (task?.state === "interrupted") {
      throw new ApiError("服务已重启，本次请求尚未完成，请重新执行", "task_interrupted", 503);
    }
    if (task?.state === "cancelled") {
      throw new ApiError("查询已停止", "query_cancelled", 499);
    }
    if (Number(task.http_status) >= 400) {
      const code = task.result?.error?.code || "request_failed";
      throw new ApiError(safeErrorMessage(code, task.http_status), code, task.http_status, task.result?.request_id || "");
    }
    if (!task.result || typeof task.result !== "object") throw new ApiError("任务结果无效", "invalid_response", 502);
    return task.result;
  }

  async function postTurnoverReport(profileId, requestKey) {
    const body = {
      conversation_id: state.backendConversationId,
      profile_id: profileId
    };
    let payload = await fetchJsonWithRetry("/v1/reports/center-warehouse-turnover", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
        "Idempotency-Key": requestKey
      },
      body: JSON.stringify(body)
    }, CHAT_TIMEOUT_MS, true);
    let polls = 0;
    while (payload?.status === "running" && polls < REQUEST_POLL_LIMIT) {
      polls += 1;
      await waitForRequestReceipt();
      payload = await fetchJsonWithRetry("/v1/reports/center-warehouse-turnover", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": state.csrfToken,
          "Idempotency-Key": requestKey
        },
        body: JSON.stringify(body)
      }, HEALTH_TIMEOUT_MS, true);
    }
    if (payload?.status === "running") {
      throw new ApiError(safeErrorMessage("request_in_progress", 202), "request_in_progress", 202);
    }
    return payload;
  }

  function canGenerateReportFromEvent(event) {
    const hasRows = [event?.businessTable, ...(event?.businessTables || [])].some((table) => table?.rows?.length);
    return Boolean(
      event?.kind === "execution" &&
      event.requestId &&
      event.outcome === "completed" &&
      event.queryStatus === "succeeded" &&
      ["succeeded", "live_success", "summary_only", "batch_complete"].includes(event.resultState) &&
      hasRows
    );
  }

  function reportSourceLabel(session, event) {
    const question = String(event?.question || "未命名查询").replace(/\s+/g, " ").trim();
    const timestamp = event?.createdAt ? new Date(event.createdAt).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    }) : "";
    return String(session?.title || "取数会话") + " · " + timestamp + " · " + question.slice(0, 90);
  }

  function collectReportSources() {
    const sources = [];
    state.sessions.forEach((session) => {
      (session.events || []).forEach((event, eventIndex) => {
        if (!canGenerateReportFromEvent(event)) return;
        sources.push({
          key: "source-" + sources.length,
          session,
          event,
          eventIndex,
          label: reportSourceLabel(session, event)
        });
      });
    });
    return sources.sort((a, b) => Number(b.event.createdAt || 0) - Number(a.event.createdAt || 0));
  }

  function closeReportDialog() {
    if (elements.reportDialog?.open) elements.reportDialog.close();
    elements.reportEntryButton.focus();
  }

  function renderReportDialogState() {
    const source = reportDialogSources.find((item) => item.key === elements.reportSourceSelect.value);
    const busy = reportDialogBusy || reportJobs.has(source?.event.requestId);
    elements.reportForm.setAttribute("aria-busy", String(busy));
    elements.reportSourceSelect.disabled = busy || !reportDialogSources.length;
    [elements.reportTitleInput, elements.reportAudienceInput, elements.reportInstructionsInput].forEach((input) => {
      input.disabled = busy || !source;
    });
    elements.reportSubmit.disabled = busy || !source;
    elements.reportSubmit.hidden = !reportDialogSources.length;
    elements.reportSubmit.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#icon-chart" /></svg>${busy ? "正在生成报告…" : "生成报告"}`;
    elements.reportQueryButton.hidden = reportDialogSources.length > 0;
    elements.reportQueryButton.disabled = state.loading || !state.sessionReady;
    const files = source?.event.report?.files || [];
    elements.reportDialogDownloads.hidden = !files.length;
    elements.reportDialogDownloads.innerHTML = files.map((file) =>
      '<a class="report-download-link" href="' + escapeHtml(file.url) + '" download="' + escapeHtml(file.filename) + '">' +
      escapeHtml(({excel:"Excel",word:"Word",csv:"CSV",png:"PNG",pdf:"PDF",html:"HTML"})[file.format] || file.format) + '</a>'
    ).join("");
  }

  function openReportDialog() {
    if (!state.sessionReady) return;
    if (reportDialogBusy) {
      closeSidebar();
      elements.reportDialog.showModal();
      return;
    }
    reportDialogSources = collectReportSources();
    elements.reportSourceSelect.innerHTML = reportDialogSources.map((source) =>
      '<option value="' + escapeHtml(source.key) + '">' + escapeHtml(source.label) + "</option>"
    ).join("");
    const current = reportDialogSources.find((source) => source.session.id === state.activeId);
    if (current) elements.reportSourceSelect.value = current.key;
    elements.reportSourceEmpty.hidden = reportDialogSources.length > 0;
    elements.reportTitleInput.value = "";
    elements.reportAudienceInput.value = "供应链业务";
    elements.reportInstructionsInput.value = "";
    elements.reportDialogStatus.hidden = true;
    renderReportDialogState();
    closeSidebar();
    elements.reportDialog.showModal();
  }

  async function submitReportDialog(event) {
    event.preventDefault();
    if (reportDialogBusy || !elements.reportForm.reportValidity()) return;
    const source = reportDialogSources.find((item) => item.key === elements.reportSourceSelect.value);
    if (!source || !state.sessions.includes(source.session) || !source.session.events.includes(source.event) || !canGenerateReportFromEvent(source.event)) {
      elements.reportDialogStatus.textContent = "查询结果已不可用，请重新选择数据来源。";
      elements.reportDialogStatus.hidden = false;
      return;
    }
    reportDialogBusy = true;
    elements.reportSubmit.disabled = true;
    const title = String(elements.reportTitleInput.value || "").trim().slice(0, 160);
    const audience = String(elements.reportAudienceInput.value || "供应链业务").trim().slice(0, 120) || "供应链业务";
    const instructions = String(elements.reportInstructionsInput.value || "").trim().slice(0, 2000);
    elements.reportDialogStatus.textContent = "正在生成报告…";
    elements.reportDialogStatus.dataset.state = "running";
    elements.reportDialogStatus.hidden = false;
    renderReportDialogState();
    try {
      const result = await generateReport(source.session, source.event, source.eventIndex, instructions, { title, audience });
      elements.reportDialogStatus.textContent = result?.report ? "报告已生成。" : result?.error || "报告生成失败，请重试。";
      elements.reportDialogStatus.dataset.state = result?.report ? "completed" : "failed";
    } finally {
      reportDialogBusy = false;
      renderReportDialogState();
      updateComposerState();
    }
  }

  async function generateReport(session, event, eventIndex, instructions = "", options = {}) {
    if (!event?.requestId) return { error: "查询结果已不可用，请重新查询。" };
    if (reportJobs.has(event.requestId)) return { error: "此查询的报告正在生成，请稍候。" };
    reportJobs.add(event.requestId);
    renderMessages();
    try {
      const reportTitle = String(options?.title || "业务数据分析报告").trim().slice(0, 160) || "业务数据分析报告";
      const reportAudience = String(options?.audience || "供应链业务").trim().slice(0, 120) || "供应链业务";
      const payload = await fetchJsonWithRetry("/v1/reports", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": state.csrfToken,
        },
        body: JSON.stringify({
          conversation_id: state.backendConversationId,
          request_id: event.requestId,
          title: reportTitle,
          audience: reportAudience,
          instructions: String(instructions || "").trim().slice(0, 2000),
          formats: ["html", "pdf", "word", "excel", "csv", "png"],
        }),
      }, CHAT_TIMEOUT_MS, true);
      const report = normalizeReport(payload);
      if (!report) throw new ApiError("报告返回结果无效", "invalid_response", 502);
      event.report = report;
      persistUiHistory();
      showToast("报告已生成，可下载不同格式文件");
      return { report };
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "报告生成失败，请稍后重试";
      showToast(message);
      return { error: message };
    } finally {
      reportJobs.delete(event.requestId);
      if (state.sessions.includes(session)) {
        renderMessages();
        persistUiHistory();
      }
      if (elements.reportDialog.open) renderReportDialogState();
    }
  }

  async function bootstrapSession() {
    clearTransientInputs();
    state.sessionReady = false;
    state.identityMode = "";
    state.csrfToken = "";
    state.backendConversationId = "";
    state.historyStorageKey = "";
    state.sessions = [];
    state.activeId = null;
    state.nextSessionNumber = 1;
    state.contextState = null;
    state.learningSummary = null;
    resetInspector();
    renderLearningSummary(null);
    render();
    updateComposerState();
    try {
      // The server resolves the canonical conversation for this installation.
      // The conversation id is also the namespace for local display history.
      const payload = await fetchJsonWithRetry(
        "/v1/session",
        { headers: { Accept: "application/json" } },
        HEALTH_TIMEOUT_MS,
        true
      );
      if (!validSessionPayload(payload)) throw new ApiError("会话初始化失败，请刷新页面重试", "invalid_session");
      state.csrfToken = payload.csrf_token;
      state.sessionReady = true;
      state.identityMode = payload.identity_mode || "installation";
      state.backendConversationId = payload.conversation_id || await allocateBackendConversation();
      const restored = restoreUiHistory(state.backendConversationId);
      const pendingDrafts = restored ? state.sessions.flatMap((item) => item.events)
        .filter((event) => event.kind === "clarification" && event.clarification?.draft_id) : [];
      const session = restored ? activeSession() : createSession();
      if (!restored) {
        state.sessions = [session];
        state.activeId = session.id;
      } else {
        // A clarification token is one-time and server-owned. Drop stale
        // cards before asking the server whether one is still pending.
        state.sessions.forEach((item) => {
          item.events = item.events.filter((event) => event.kind !== "clarification");
          item.pendingClarification = null;
        });
      }
      elements.sidebarStatusText.textContent = identityBindingLabel();
      const context = payload.context_state
        ? normalizeContext(payload.context_state)
        : normalizeContext({ inherited: Boolean(payload.conversation_id), compressed: false, enabled: true });
      session.contextState = context;
      updateContextStatus(context);
      if (payload.clarification_pending === true || payload.clarification_pending?.required === true) {
        try {
          const resumed = await resumePendingClarification();
          if (resumed.clarification) {
            const draft = pendingDrafts.find((event) => event.clarification.draft_id === resumed.clarification.draft_id);
            upsertPendingClarification(
              session,
              resumed.clarification,
              resumed.context,
              true,
              draft?.sourceQuestion || latestQuestionForSession(session)
            );
            const event = pendingClarificationEvent(session);
            if (event && draft) event.selectedAnswers = normalizeStoredAnswers(draft.selectedAnswers, resumed.clarification);
          }
        } catch (resumeError) {
          if (resumeError?.code === "session_security_error") throw resumeError;
          showToast("当前口径选择暂时无法恢复，请重新提交问题");
        }
      }
      persistUiHistory();
      refreshLearningSummary(false);
    } catch (error) {
      state.sessionReady = false;
      state.identityMode = "";
      state.csrfToken = "";
      state.backendConversationId = "";
      clearTransientInputs();
      showToast(error instanceof Error ? error.message : "会话初始化失败");
    }
    render();
    updateComposerState();
    if (state.sessionReady) {
      restoreInspectorFromSession();
      void loadRuleCatalog();
      window.setTimeout(resumeInterruptedRequest, 0);
      window.setTimeout(resumeInterruptedDrilldowns, 0);
      window.setTimeout(() => proposalReview.resumeSession(activeSession()), 0);
      window.setTimeout(() => interpretation.resumeSession(activeSession()), 0);
    }
  }

  function ensureSharedConversation(payload) {
    if (typeof payload?.conversation_id !== "string" || !payload.conversation_id || payload.conversation_id !== state.backendConversationId) {
      throw new ApiError("会话上下文校验失败，请刷新页面重试", "session_security_error", 403);
    }
  }

  function ruleValueForField(field) {
    if (field.default !== undefined) return field.default;
    if (field.type === "multi_select") return [];
    return "";
  }

  function normalizeRuleSubmission(value) {
    if (!value || typeof value !== "object" || value.executionMode !== "rule" ||
        !/^[A-Za-z0-9_-]{1,80}$/.test(String(value.ruleId || ""))) return null;
    const raw = value.ruleParams && typeof value.ruleParams === "object" ? value.ruleParams : {};
    const ruleParams = {};
    Object.entries(raw).slice(0, 32).forEach(([key, item]) => {
      if (!/^[A-Za-z0-9_-]{1,80}$/.test(key)) return;
      if (Array.isArray(item)) {
        ruleParams[key] = item.filter((entry) => typeof entry === "string").slice(0, 100)
          .map((entry) => entry.slice(0, 200));
      } else if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") {
        ruleParams[key] = typeof item === "string" ? item.slice(0, 200) : item;
      }
    });
    return { executionMode: "rule", ruleId: String(value.ruleId), ruleParams };
  }

  function renderRuleCatalog(catalog) {
    ruleState.catalog = catalog;
    const rules = Array.isArray(catalog?.rules) ? catalog.rules : [];
    elements.ruleCatalogVersion.textContent = catalog?.version ? `版本 ${catalog.version}` : "已核验";
    elements.ruleCatalog.innerHTML = rules.map((rule) => (
      `<button class="rule-card" type="button" role="listitem" data-rule-id="${escapeHtml(rule.id)}">` +
        `<span class="rule-card-title">${escapeHtml(rule.title)}</span>` +
        `<span class="rule-card-description">${escapeHtml(rule.description)}</span>` +
        `<svg class="icon" aria-hidden="true"><use href="#icon-chevron" /></svg>` +
      `</button>`
    )).join("") || `<span class="rule-catalog-loading">暂无可执行规则</span>`;
    elements.ruleCatalog.querySelectorAll("[data-rule-id]").forEach((button) => {
      button.addEventListener("click", () => openRuleForm(button.dataset.ruleId));
    });
  }

  function ruleFieldMarkup(field) {
    const id = escapeHtml(field.id);
    const label = escapeHtml(field.label || field.id);
    const help = field.help ? `<small>${escapeHtml(field.help)}</small>` : "";
    if (field.type === "select") {
      const options = (Array.isArray(field.options) ? field.options : []).map((option) => (
        `<option value="${escapeHtml(option.id)}"${String(option.id) === String(field.default ?? "") ? " selected" : ""}>${escapeHtml(option.label)}</option>`
      )).join("");
      return `<label class="rule-field"><span>${label}</span><select data-rule-field="${id}">${options}</select>${help}</label>`;
    }
    if (field.type === "multi_select") {
      const options = (Array.isArray(field.options) ? field.options : []).map((option) => (
        `<label class="rule-check"><input type="checkbox" value="${escapeHtml(option.id)}" data-rule-field="${id}" /><span>${escapeHtml(option.label)}</span></label>`
      )).join("");
      return `<fieldset class="rule-field rule-multi"><legend>${label}</legend><input class="rule-search" type="search" data-rule-search="${id}" placeholder="搜索${label}" autocomplete="off" />` +
        `<div class="rule-check-grid">${options}</div>${help}</fieldset>`;
    }
    return `<label class="rule-field"><span>${label}</span><input type="${field.type === "date" ? "date" : "text"}" data-rule-field="${id}" placeholder="${field.type === "date" ? "YYYY-MM-DD" : ""}" />${help}</label>`;
  }

  function refreshRuleDateFields() {
    const windowInput = elements.ruleFields.querySelector('[data-rule-field="window"]');
    const custom = windowInput?.value === "custom";
    elements.ruleFields.querySelectorAll('[data-rule-custom-date="true"]').forEach((field) => {
      field.hidden = !custom;
      field.querySelector("input")?.toggleAttribute("required", custom);
    });
  }

  function openRuleForm(ruleId) {
    const rule = ruleState.catalog?.rules?.find((item) => item.id === ruleId);
    if (!rule) return;
    ruleState.selected = rule;
    elements.ruleFormTitle.textContent = rule.title;
    elements.ruleFormDescription.textContent = rule.description;
    elements.ruleFields.innerHTML = (rule.fields || []).map((field) => {
      const customDate = ["start_date", "end_date"].includes(field.id) && rule.fields.some((item) => item.id === "window");
      return `<div${customDate ? ' data-rule-custom-date="true"' : ""}>${ruleFieldMarkup(field)}</div>`;
    }).join("");
    elements.ruleForm.hidden = false;
    elements.ruleCatalog.setAttribute("aria-hidden", "true");
    elements.ruleCatalog.setAttribute("inert", "");
    elements.ruleFields.querySelector('[data-rule-field="window"]')?.addEventListener("change", refreshRuleDateFields);
    elements.ruleFields.querySelectorAll("[data-rule-search]").forEach((input) => {
      input.addEventListener("input", () => {
        const query = input.value.trim().toLocaleLowerCase();
        input.closest(".rule-multi")?.querySelectorAll(".rule-check").forEach((item) => {
          item.hidden = query !== "" && !item.textContent.toLocaleLowerCase().includes(query);
        });
      });
    });
    refreshRuleDateFields();
    window.requestAnimationFrame(() => {
      elements.ruleFields.querySelector("input, select, textarea, button")?.focus({ preventScroll: true });
    });
  }

  function closeRuleForm() {
    ruleState.selected = null;
    elements.ruleForm.hidden = true;
    elements.ruleCatalog.removeAttribute("aria-hidden");
    elements.ruleCatalog.removeAttribute("inert");
  }

  function readRuleParams() {
    const params = {};
    elements.ruleFields.querySelectorAll("[data-rule-field]").forEach((input) => {
      const fieldId = input.dataset.ruleField;
      if (input.type === "checkbox") {
        if (!Array.isArray(params[fieldId])) params[fieldId] = [];
        if (input.checked) params[fieldId].push(input.value);
      } else {
        params[fieldId] = input.value;
      }
    });
    return params;
  }

  function ruleDisplayQuestion(rule, params) {
    const details = (rule.fields || []).map((field) => {
      const value = params[field.id];
      if (Array.isArray(value)) {
        if (!value.length) return "";
        const labels = value.map((item) => field.options?.find((option) => String(option.id) === String(item))?.label || item);
        return `${field.label}: ${labels.join("、")}`;
      }
      if (value === undefined || value === null || value === "") return "";
      const label = field.options?.find((option) => String(option.id) === String(value))?.label || value;
      return `${field.label}: ${label}`;
    }).filter(Boolean);
    return details.length ? `${rule.title}（${details.join("；")}）` : rule.title;
  }

  async function loadRuleCatalog() {
    if (!state.sessionReady) return;
    try {
      renderRuleCatalog(await fetchJson("/v1/rules", {}, HEALTH_TIMEOUT_MS));
    } catch (_error) {
      elements.ruleCatalogVersion.textContent = "暂不可用";
      elements.ruleCatalog.innerHTML = `<span class="rule-catalog-loading">规则加载失败，请刷新后重试</span>`;
    }
  }

  function submitRule(event) {
    event.preventDefault();
    const rule = ruleState.selected;
    if (!rule || state.loading || !state.sessionReady) return;
    const form = elements.ruleForm;
    if (!form.reportValidity()) return;
    const params = readRuleParams();
    const displayQuestion = ruleDisplayQuestion(rule, params);
    void sendQuestion(displayQuestion, null, "", {
      executionMode: "rule",
      ruleId: rule.id,
      ruleParams: params,
      displayQuestion
    });
  }

  async function sendQuestion(rawQuestion, clarificationAnswers = null, clarificationToken = "", submission = null) {
    if (proposalReview.blocked(activeSession()) || interpretation.blocked(activeSession())) return;
    if (state.loading || conversionReading || !state.sessionReady) return;
    const executionMode = submission?.executionMode || "natural_language";
    const ruleSubmission = normalizeRuleSubmission(submission);
    const question = String(rawQuestion || submission?.displayQuestion || "").trim();
    const detailBatch = normalizeDetailSelection(submission?.detailBatch);
    if (!question && !Array.isArray(clarificationAnswers)) {
      elements.questionInput.focus();
      return;
    }
    const session = submission?.session || activeSession();
    const pendingEvent = submission?.event || null;
    const isClarificationSubmission = Array.isArray(clarificationAnswers);
    // Report execution is an explicit rule submission. Natural-language input
    // must never inherit a report hint left by an older UI state.
    let reportKind = executionMode === "rule" ? (submission?.reportKind || null) : null;
    if (executionMode !== "rule" && !pendingEvent && !isClarificationSubmission) {
      session.reportKind = null;
      session.reportProfileId = null;
    }
    if (pendingEvent && pendingEvent.clarification?.questions?.some((item) => item.id === "turnover_inventory_profile")) {
      reportKind = REPORT_KIND;
      session.reportKind = REPORT_KIND;
    }
    const selectedProfile = profileFromAnswers(clarificationAnswers);
    if (selectedProfile) session.reportProfileId = selectedProfile;
    if (pendingEvent) pendingEvent.submitting = true;
    const requestNonce = ++state.requestNonce;
    const requestKey = submission?.requestKey || randomId("req");
    const displayQuestion = displayQuestionText(question) || displayQuestionText(
      submission?.sourceQuestion || pendingEvent?.sourceQuestion || latestQuestionForSession(session)
    );
    const startedAt = performance.now();
    if (!pendingEvent && !isClarificationSubmission && displayQuestion && !submission?.reuseQuestion) {
      session.events.push({
        kind: "question",
        question: displayQuestion,
        createdAt: Date.now()
      });
    }
    const runningEvent = {
      kind: "running",
      ...(ruleSubmission || {}),
      detailBatch,
      conversionSubmitted: Boolean(submission?.conversionInput) || submission?.conversionSubmitted === true,
      requestKey,
      question: displayQuestion || question,
      outcome: "running",
      context: state.contextState || normalizeContext(null),
      process: normalizeProcess(null, "running"),
      startedAt
    };
    session.events.push(runningEvent);
    if (runningEvent.conversionSubmitted) {
      runningEvent.stageLabel = "正在检查转换材料";
      runningEvent.process = [];
    }
    if (!pendingEvent) session.pendingClarification = null;
    setLoading(true);
    render();
    updateInspector(runningEvent, startedAt);
    const body = { conversation_id: state.backendConversationId };
    body.execution_mode = executionMode;
    if (executionMode === "rule") {
      body.rule_id = submission.ruleId;
      body.rule_params = submission.ruleParams || {};
    }
    if (submission?.conversionInput && !detailBatch) body.sql_conversion = submission.conversionInput;
    if (question && !detailBatch) body.question = question;
    if (detailBatch) body.detail_batch = detailBatch;
    if (Array.isArray(clarificationAnswers)) {
      body.clarification_answers = clarificationAnswers;
      if (clarificationToken) body.clarification_token = clarificationToken;
    }
    if (executionMode === "rule" && reportKind && !detailBatch) body.report_kind = reportKind;
    if (executionMode === "rule" && session.reportProfileId && !detailBatch) body.report_profile_id = session.reportProfileId;
    elements.questionInput.value = "";
    updateComposerState();
    let recoveredAutomatically = false;
    let recoveredClarification = null;
    let recoveredContext = null;
    let staleClarificationCleared = false;
    try {
      let payload;
      try {
        payload = await postChatRequest(body, requestKey, submission?.taskId || "");
      } catch (initialError) {
        const shouldResume = (
          CLARIFICATION_RECOVERY_CODES.has(initialError?.code) &&
          (isClarificationSubmission || initialError?.code === "clarification_pending")
        );
        if (!shouldResume) throw initialError;

        let recovered;
        try {
          recovered = await resumePendingClarification();
        } catch (resumeError) {
          if (initialError?.code === "clarification_invalid") staleClarificationCleared = Boolean(pendingEvent);
          if (resumeError?.code === "session_security_error") throw resumeError;
          throw initialError;
        }

        if (recovered.clarification && !isClarificationSubmission) {
          removeEvent(session, runningEvent);
          if (pendingEvent) removeEvent(session, pendingEvent);
           upsertPendingClarification(
             session,
             recovered.clarification,
             recovered.context,
             true,
             latestQuestionForSession(session)
           );
          showToast("请先完成当前口径选择");
          return;
        }

        if (recovered.clarification && !clarificationAcceptsAnswers(recovered.clarification, clarificationAnswers)) {
          removeEvent(session, runningEvent);
          if (pendingEvent) removeEvent(session, pendingEvent);
           upsertPendingClarification(
             session,
             recovered.clarification,
             recovered.context,
             true,
             latestQuestionForSession(session)
           );
          showToast("口径选项确有变化，请重新选择");
          return;
        }

        if (recovered.clarification) {
          recoveredClarification = recovered.clarification;
          recoveredContext = recovered.context;
          payload = await postChatRequest({
            ...body,
            clarification_token: recovered.clarification.token
          }, requestKey);
          recoveredAutomatically = true;
        } else if (isClarificationSubmission && reportKind === REPORT_KIND && session.reportProfileId) {
          staleClarificationCleared = Boolean(pendingEvent);
          // Keep the report call on the same logical receipt as this selection:
          // postTurnoverReport(session.reportProfileId)
          payload = await postTurnoverReport(session.reportProfileId, requestKey);
          recoveredAutomatically = true;
        } else {
          staleClarificationCleared = Boolean(pendingEvent);
          throw initialError;
        }
      }

      // A late response from a superseded request must not append a second
      // result or error to the active conversation.
      if (!ownsRequest(session, requestNonce, runningEvent)) return;
      ensureSharedConversation(payload);
      const normalized = normalizeResponse(payload);
      if (detailBatch && (payload.detail_batch_source?.result_id !== detailBatch.result_id
        || payload.detail_batch_source?.batch_index !== detailBatch.batch_index
        || normalized.businessTable?.detail_batches?.result_id !== detailBatch.result_id
        || normalized.businessTable.detail_batches.next_batch !== detailBatch.batch_index + 1
        || payload.result_state !== (normalized.businessTable.detail_batches.complete ? "live_success" : "batch_complete"))) {
        throw new ApiError(safeErrorMessage("query_invalid_result", 502), "query_invalid_result", 502);
      }
      if (pendingEvent) removeEvent(session, pendingEvent);
      if (normalized.clarification) {
        removeEvent(session, runningEvent);
        upsertPendingClarification(
          session,
          normalized.clarification,
          normalized.context,
          true,
          displayQuestion
        );
        const clarificationEvent = pendingClarificationEvent(session);
        if (clarificationEvent) clarificationEvent.requestKey = requestKey;
      } else {
        session.events = session.events.filter((event) => (
          event !== runningEvent && event.requestKey !== requestKey && event.kind !== "clarification"
        ));
        session.pendingClarification = null;
        session.events.push({
          kind: "execution",
          ...(ruleSubmission || {}),
          requestId: normalized.requestId,
          question: displayQuestion || question,
          outcome: normalized.outcome,
          resultState: normalized.resultState,
          queryStatus: normalized.queryStatus,
          outcomeLabel: normalized.outcomeLabel,
          process: normalized.process,
          statusMessage: normalized.statusMessage,
          context: normalized.context,
          businessResult: normalized.businessResult,
          businessTable: normalized.businessTable,
          businessTables: normalized.businessTables,
          validationWarnings: normalized.validationWarnings,
          guidance: normalized.guidance,
          artifacts: normalized.artifacts,
          report: normalized.report,
          feedback: null
        });
        session.events[session.events.length - 1].requestKey = requestKey;
        session.reportKind = null;
        session.reportProfileId = null;
      }
      session.contextState = normalized.context;
      state.contextState = normalized.context;
      if (state.activeId === session.id && state.requestNonce === requestNonce) updateInspector(normalized, startedAt);
      void refreshLearningSummary(false);
      if (recoveredAutomatically) showToast("已沿用本次口径选择继续执行");
    } catch (error) {
      if (!ownsRequest(session, requestNonce, runningEvent)) return;
      removeEvent(session, runningEvent);
      if (recoveredClarification) {
        upsertPendingClarification(
          session,
          recoveredClarification,
          recoveredContext,
          true,
          displayQuestion
        );
        const recoveredEvent = pendingClarificationEvent(session);
        if (recoveredEvent) {
          recoveredEvent.submitting = false;
          recoveredEvent.selectedAnswers = cloneClarificationAnswers(clarificationAnswers);
          recoveredEvent.selectionConfirmed = true;
        }
      } else if (pendingEvent && staleClarificationCleared) {
        removeEvent(session, pendingEvent);
        session.pendingClarification = pendingClarificationEvent(session)?.clarification || null;
      } else if (pendingEvent) {
        pendingEvent.submitting = false;
        pendingEvent.selectedAnswers = cloneClarificationAnswers(clarificationAnswers);
        pendingEvent.selectionConfirmed = true;
        if (!session.events.includes(pendingEvent)) session.events.push(pendingEvent);
        session.pendingClarification = cloneClarification(submission?.clarification || pendingEvent.clarification);
      } else {
        session.pendingClarification = pendingClarificationEvent(session)?.clarification || null;
      }
      const errorEvent = {
        kind: "error",
        ...(ruleSubmission || {}),
        detailBatch,
        conversionSubmitted: runningEvent.conversionSubmitted,
        requestKey,
        taskId: runningEvent.taskId || submission?.taskId || "",
        requestId: boundedText(error?.requestId, 200),
        question: displayQuestion || question,
        text: error instanceof Error ? error.message : "本次请求未完成，请稍后重试",
        errorCode: storedErrorCode(error?.code),
        errorStatus: storedErrorStatus(error?.status)
      };
      const existingErrorIndex = requestEventIndex(session, requestKey);
      if (existingErrorIndex >= 0) session.events[existingErrorIndex] = errorEvent;
      else session.events.push(errorEvent);
      if (state.activeId === session.id && state.requestNonce === requestNonce) {
        const failedContext = session.contextState || state.contextState || normalizeContext(null);
        const errorOutcome = error?.code === "business_definition_unpublished" ? "blocked" : "failed";
        updateInspector({
          outcome: errorOutcome,
          resultState: error?.code,
          outcomeLabel: resultLabel(errorOutcome, error?.code),
          context: failedContext,
          process: normalizeProcess(null, errorOutcome)
        }, startedAt);
      }
      if (error?.code === "session_security_error") {
        invalidateSession(error.message);
      }
      void refreshLearningSummary(false);
      showToast(error instanceof Error ? error.message : "本次请求未完成");
    } finally {
      if (state.requestNonce === requestNonce) {
        setLoading(false);
        render();
        updateComposerState();
      }
    }
  }

  function resumeInterruptedRequest() {
    if (!state.sessionReady || state.loading) return;
    const session = activeSession();
    const event = [...session.events].reverse().find((item) => (
      item?.kind === "error" &&
      item.errorCode === "reload_interrupted" &&
      item.requestKey &&
      item.question
    ));
    if (!event) return;
    if (event.conversionSubmitted && !event.taskId) {
      showToast("原始转换材料未保留，请重新提交材料");
      return;
    }
    removeEvent(session, event);
    render();
    void sendQuestion(event.question, null, "", {
      session,
      reuseQuestion: true,
      ...(normalizeRuleSubmission(event) || {}),
      requestKey: event.requestKey,
      taskId: event.taskId,
      detailBatch: event.detailBatch,
      conversionSubmitted: event.conversionSubmitted === true
    });
  }

  function submitClarification(index) {
    if (state.loading) return;
    const session = activeSession();
    const event = session.events[index];
    const clarification = event?.kind === "clarification" ? event.clarification : null;
    if (!clarification) return;
    if (event.submitting) return;
    if (!clarification.token) {
      showToast("口径选项已过期，请重新提交问题");
      return;
    }
    const cardRoot = elements.messageList.querySelector(`[data-clarification-index="${index}"]`);
    if (!cardRoot) return;
    const selections = readClarificationChoices(cardRoot, clarification);
    event.selectedAnswers = selections;
    persistUiHistory();
    const missing = clarification.questions.find((question) => !selections.some((answer) => answer.question_id === question.id));
    const invalidRange = selections.find((answer) => answer.option_ids.includes(DATE_RANGE_OPTIONS[answer.question_id]) && !validDateRange(answer.date_range));
    if (missing || invalidRange) {
      const message = missing ? "请完成全部口径选择后继续" : "请选择有效的起止日期，结束日期必须晚于开始日期（结束日不计入）。";
      const error = cardRoot.querySelector(".clarification-error");
      error.textContent = message;
      error.hidden = false;
      const target = missing
        ? [...cardRoot.querySelectorAll("input[data-question-id]")].find((input) => input.dataset.questionId === missing.id)
        : [...cardRoot.querySelectorAll("input[data-date-question]")].find((input) => input.dataset.dateQuestion === invalidRange.question_id && (!input.value || input.dataset.datePart === "end"));
      target?.setAttribute("aria-invalid", "true");
      target?.setAttribute("aria-describedby", error.id);
      target?.focus();
      return;
    }
    // Keep the card in the event list until the backend accepts the selection.
    // This leaves a retryable, user-visible choice when the request fails.
    event.submitting = true;
    render();
    void sendQuestion("", selections, clarification.token, {
      session,
      event,
      clarification,
      sourceQuestion: event.sourceQuestion || latestQuestionForSession(session)
    });
  }

  function updateComposerState() {
    const session = activeSession();
    const pending = state.recoveringClarification || Boolean(pendingClarificationForSession(session));
    const value = elements.questionInput.value;
    elements.charCount.textContent = `${value.length} / 4000`;
    elements.sendButton.disabled = state.loading || conversionReading || !state.sessionReady || pending || !value.trim();
    const runningEvent = [...session.events].reverse().find((event) => event?.kind === "running");
    elements.stopButton.hidden = !state.loading;
    elements.stopButton.disabled = runningEvent?.canceling === true;
    elements.stopButton.title = runningEvent?.canceling ? "正在停止查询" : "停止查询";
    elements.stopButton.setAttribute("aria-label", elements.stopButton.title);
    elements.reportEntryButton.disabled = !state.sessionReady;
    elements.conversionUpload.disabled = state.loading || conversionReading || !state.sessionReady || pending;
    const proposalBusy = proposalReview.blocked(session) || interpretation.blocked(session);
    elements.proposalUpload.disabled = state.loading || conversionReading || !state.sessionReady || pending || proposalBusy;
    elements.conversionUpload.disabled ||= proposalBusy;
    elements.sendButton.disabled ||= proposalBusy;
    elements.questionInput.disabled = state.loading || !state.sessionReady || pending;
    const ruleBusy = state.loading || !state.sessionReady || pending;
    elements.ruleCatalog.querySelectorAll("button[data-rule-id]").forEach((button) => {
      button.disabled = ruleBusy;
    });
    elements.ruleFormClose.disabled = ruleBusy;
    elements.ruleSubmit.disabled = ruleBusy;
    elements.questionInput.placeholder = pending ? "请在上方一次完成口径选择" : "描述你要查询的指标、范围和时间…";
    const meaningMode = document.querySelector('input[name="composerMode"]:checked')?.value === "meaning";
    if (!pending && meaningMode) elements.questionInput.placeholder = "描述要新增或调整的业务口径…";
    document.querySelector('label[for="questionInput"]').textContent = meaningMode ? "输入口径需求" : "输入取数问题";
    elements.sendButton.title = meaningMode ? "提交口径需求" : "提交取数问题";
    elements.sendButton.setAttribute("aria-label", elements.sendButton.title);
    document.querySelectorAll('input[name="composerMode"]').forEach(input => {input.disabled = state.loading || pending || !state.sessionReady;});
    elements.questionInput.style.height = "auto";
    elements.questionInput.style.height = `${Math.min(elements.questionInput.scrollHeight, 150)}px`;
  }

  function startNewSession() {
    if (!state.sessionReady || state.loading) return;
    const pendingSource = state.sessions.find((item) => pendingClarificationForSession(item));
    const pending = pendingSource ? pendingClarificationForSession(pendingSource) : null;
    const pendingQuestion = pendingSource ? latestQuestionForSession(pendingSource) : "";
    const hasUsablePendingToken = Boolean(pending?.token);
    const pendingContext = pendingSource?.contextState || state.contextState || normalizeContext(null);
    if (pending && !hasUsablePendingToken) {
      // Persisted cards intentionally discard one-time tokens. Do not copy an
      // expired local card into the new view; the server is the authority.
      state.sessions.forEach((item) => {
        item.events = item.events.filter((event) => event.kind !== "clarification");
        item.pendingClarification = null;
      });
    }
    clearTransientInputs();
    const session = createSession();
    state.sessions.unshift(session);
    state.activeId = session.id;
    session.contextState = state.contextState || pendingContext;
    if (pending && hasUsablePendingToken) {
      upsertPendingClarification(
        session,
        pending,
        pendingContext,
        false,
        pendingQuestion
      );
    }
    if (pending && !hasUsablePendingToken) {
      state.recoveringClarification = true;
      void resumePendingClarification()
        .then((resumed) => {
          if (resumed.clarification) {
            upsertPendingClarification(
              session,
              resumed.clarification,
              resumed.context,
              false,
              pendingQuestion
            );
          }
        })
        .catch((error) => {
          if (error?.code !== "session_security_error") {
            showToast("当前口径选择暂时无法恢复，请重新提交问题");
          }
        })
        .finally(() => {
          state.recoveringClarification = false;
          persistUiHistory();
          render();
          updateComposerState();
        });
    }
    resetInspector();
    render();
    updateComposerState();
    elements.questionInput.focus();
    closeSidebar();
    showToast("已创建新的界面会话，共享本机上下文仍保持继承");
  }

  function invalidateSession(message = "会话已失效，请刷新页面重试") {
    state.requestNonce += 1;
    state.loading = false;
    clearPersistedUiHistory();
    state.sessionReady = false;
    state.identityMode = "";
    state.csrfToken = "";
    state.backendConversationId = "";
    state.historyStorageKey = "";
    clearTransientInputs();
    state.sessions = [];
    state.activeId = null;
    resetInspector();
    render();
    updateComposerState();
    if (message) showToast(message);
  }

  function closeSidebar() {
    const wasOpen = elements.sidebar.classList.contains("is-open");
    elements.sidebar.classList.remove("is-open");
    elements.mobileBackdrop.classList.remove("sidebar-visible");
    elements.sidebarToggle.setAttribute("aria-expanded", "false");
    if (wasOpen && state.panelReturnFocus?.focus) state.panelReturnFocus.focus();
    state.panelReturnFocus = null;
  }

  function openSidebar() {
    state.panelReturnFocus = document.activeElement;
    elements.sidebar.classList.add("is-open");
    elements.mobileBackdrop.classList.add("sidebar-visible");
    elements.sidebarToggle.setAttribute("aria-expanded", "true");
    window.requestAnimationFrame(() => elements.newChatButton.focus());
  }

  function toggleInspector(open) {
    const next = typeof open === "boolean" ? open : !elements.inspector.classList.contains("is-open");
    elements.inspector.classList.toggle("is-open", next);
    elements.mobileBackdrop.classList.toggle("is-visible", next);
    elements.inspectorToggle.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) {
      state.panelReturnFocus = document.activeElement;
      window.requestAnimationFrame(() => elements.inspectorClose.focus());
    } else if (state.panelReturnFocus?.focus) {
      state.panelReturnFocus.focus();
      state.panelReturnFocus = null;
    }
  }

  function learningCount(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? Math.min(Math.floor(parsed), 999999) : 0;
  }

  function renderLearningSummary(payload = state.learningSummary) {
    const summary = payload && typeof payload === "object" ? payload : null;
    const total = learningCount(summary?.question_count);
    elements.learningCount.textContent = total > 999 ? "999+" : String(total);
    if (!summary) {
      elements.learningSummary.innerHTML = '<div class="learning-loading">正在汇总测试记录…</div>';
      return;
    }
    const routes = Array.isArray(summary.route_families) ? summary.route_families.slice(0, 8) : [];
    const routeMarkup = routes.length
      ? `<div class="learning-route-list">${routes.map((route) => {
          const id = boundedText(route?.id, 40);
          const label = LEARNING_ROUTE_LABELS[id] || "其他";
          return `<div class="learning-route"><span>${escapeHtml(label)}</span><strong>${learningCount(route?.count)}</strong></div>`;
        }).join("")}</div>`
      : '<div class="learning-empty">暂时没有测试记录</div>';
    elements.learningSummary.innerHTML =
      `<div class="learning-stat-grid">` +
      `<div class="learning-stat"><span>已记录问题</span><strong>${total}</strong></div>` +
      `<div class="learning-stat"><span>已完成</span><strong>${learningCount(summary.completed_count)}</strong></div>` +
      `<div class="learning-stat"><span>待确认口径</span><strong>${learningCount(summary.clarification_count)}</strong></div>` +
      `<div class="learning-stat"><span>需修正</span><strong>${learningCount(summary.correction_count)}</strong></div>` +
      `</div><div class="learning-section-title">问题类别</div>${routeMarkup}` +
      `<div class="learning-review-line"><span>待审核候选</span><strong>${learningCount(summary.pending_candidate_count)}</strong></div>`;
  }

  async function refreshLearningSummary(showError = false) {
    if (!state.sessionReady) return null;
    try {
      const payload = await fetchJson(LEARNING_SUMMARY_ENDPOINT, { headers: { Accept: "application/json" } }, HEALTH_TIMEOUT_MS);
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new ApiError("学习总结返回格式无效", "invalid_response");
      state.learningSummary = payload;
      renderLearningSummary(payload);
      return payload;
    } catch (error) {
      if (showError) showToast(error instanceof Error ? error.message : "学习总结暂时不可用");
      return null;
    }
  }

  async function refreshHealth() {
    try {
      const health = await fetchJson("/health", { headers: { Accept: "application/json" } }, HEALTH_TIMEOUT_MS);
      const backendReady = Boolean(health.query_backend?.ready);
      const sessionsReady = Boolean(health.sessions?.ready);
      const ready = Boolean(health.ready && backendReady && sessionsReady);
      const statusLabel = ready ? "Doris 在线" : backendReady ? "会话待检查" : "Doris 不可用";
      elements.connectionStatus.innerHTML = `<span class="status-dot ${ready ? "online" : "offline"}"></span><span>${statusLabel}</span>`;
      elements.sidebarStatusDot.className = `status-dot ${ready ? "online" : "offline"}`;
      if (state.sessionReady) elements.sidebarStatusText.textContent = ready ? `Doris 已连接 · ${identityBindingLabel()}` : statusLabel;
    } catch (_error) {
      elements.connectionStatus.innerHTML = '<span class="status-dot offline"></span><span>服务不可用</span>';
      elements.sidebarStatusDot.className = "status-dot offline";
      elements.sidebarStatusText.textContent = "聊天服务不可用";
    }
  }

  function applyTheme(theme) {
    elements.body.dataset.theme = theme;
    try { localStorage.setItem(THEME_KEY, theme); } catch (_error) { /* optional */ }
  }

  function initTheme() {
    let theme = "light";
    try { theme = localStorage.getItem(THEME_KEY) || theme; } catch (_error) { /* optional */ }
    applyTheme(theme === "dark" ? "dark" : "light");
  }

  function openDefinitionDialog() {
    if (!state.sessionReady || state.feedbackSubmitting) return;
    if (typeof elements.definitionDialog.showModal === "function") elements.definitionDialog.showModal();
    else elements.definitionDialog.setAttribute("open", "open");
    window.requestAnimationFrame(() => elements.metricNameInput.focus());
  }

  function openLearningDialog() {
    if (!state.sessionReady) return;
    renderLearningSummary(state.learningSummary);
    if (typeof elements.learningDialog.showModal === "function") elements.learningDialog.showModal();
    else elements.learningDialog.setAttribute("open", "open");
    refreshLearningSummary(true);
  }

  function closeLearningDialog() {
    if (typeof elements.learningDialog.close === "function" && elements.learningDialog.open) elements.learningDialog.close();
    else elements.learningDialog.removeAttribute("open");
  }

  function closeDefinitionDialog() {
    if (typeof elements.definitionDialog.close === "function" && elements.definitionDialog.open) elements.definitionDialog.close();
    else elements.definitionDialog.removeAttribute("open");
  }

  function clearTransientInputs() {
    proposalReview.clearInput();
    interpretation.clearInput();
    document.querySelector('input[name="composerMode"][value="query"]').checked = true;
    conversionReadNonce += 1;
    conversionReading = false;
    elements.conversionFile.value = "";
    elements.conversionInputStatus.hidden = true;
    elements.questionInput.value = "";
    elements.charCount.textContent = "0 / 4000";
    if (typeof elements.definitionForm.reset === "function") elements.definitionForm.reset();
    closeDefinitionDialog();
  }

  function feedbackPayload() {
    return {
      conversation_id: state.backendConversationId,
      metric_name: elements.metricNameInput.value.trim(),
      business_definition: elements.metricDefinitionInput.value.trim(),
      scope_and_grain: elements.metricScopeInput.value.trim(),
      unit_and_window: elements.metricUnitInput.value.trim(),
      example_question: elements.metricExampleInput.value.trim(),
      notes: elements.metricNotesInput.value.trim()
    };
  }

  async function submitDefinition(event) {
    event.preventDefault();
    if (state.feedbackSubmitting || !state.sessionReady) return;
    if (!elements.definitionForm.reportValidity()) return;
    state.feedbackSubmitting = true;
    elements.definitionSubmit.disabled = true;
    try {
      await fetchJson(FEEDBACK_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrfToken },
        body: JSON.stringify(feedbackPayload())
      }, HEALTH_TIMEOUT_MS);
      elements.definitionForm.reset();
      closeDefinitionDialog();
      showToast("已提交到口径收集箱，等待审核");
      refreshLearningSummary(false);
    } catch (error) {
      showToast(error instanceof Error ? error.message : "口径提交未完成");
    } finally {
      state.feedbackSubmitting = false;
      elements.definitionSubmit.disabled = false;
      renderSidebar();
    }
  }

  const proposalHost = {
    state, activeSession, fetchJson, randomId, avatar: assistantAvatarMarkup,
    upload: elements.proposalUpload, file: elements.proposalFile,
    container: elements.messageList,
    inputStatus: elements.proposalInputStatus, question: elements.questionInput,
    toast: showToast, persist: persistUiHistory, resetInspector,
    updateComposer: updateComposerState,
    refresh(session) {
      renderSidebar();
      if (activeSession() === session) renderMessages();
      persistUiHistory();
      updateComposerState();
    }
  };
  const proposalReview = window.RoyalProposalReview.create(proposalHost);
  const interpretation = window.RoyalInterpretation.create({...proposalHost,
    screenQuestion: displayQuestionText,
    isClosing: proposalReview.isClosing,
    reviewBlocked: proposalReview.blocked,
    handoff(session, event, proposalId) {
      if (!session.events.some(item => item.kind === "proposal" && item.requestId === event.requestId)) {
        const review = proposalReview.restore({requestId:event.requestId, proposalId, revision:0, createdAt:event.createdAt});
        session.events.splice(session.events.indexOf(event) + 1, 0, review);
      }
      // Always resume the authoritative review; interpretation carries no copy of its choices.
      void proposalReview.resumeSession(session);
    }
  });

  function submitComposer() {
    if (elements.sendButton.disabled) return;
    if (document.querySelector('input[name="composerMode"]:checked')?.value === "meaning") void interpretation.submit(elements.questionInput.value);
    else void sendQuestion(elements.questionInput.value);
  }
  elements.stopButton.addEventListener("click", () => {
    const session = activeSession();
    const event = [...session.events].reverse().find((item) => item?.kind === "running");
    if (!event || event.canceling) return;
    event.canceling = true;
    event.cancelRequested = true;
    event.cancelSent = Boolean(event.taskId);
    updateComposerState();
    renderMessages();
    if (!event.taskId) return;
    void cancelTask(event.taskId).catch((error) => {
      if (!session.events.includes(event)) return;
      event.canceling = false;
      event.cancelRequested = false;
      event.cancelSent = false;
      updateComposerState();
      renderMessages();
      showToast(error instanceof Error ? error.message : "停止查询失败，请稍后重试");
    });
  });
  elements.ruleFormClose.addEventListener("click", closeRuleForm);
  elements.ruleForm.addEventListener("submit", submitRule);
  document.querySelectorAll('input[name="composerMode"]').forEach(input => input.addEventListener("change", updateComposerState));

  elements.composerForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitComposer();
  });
  elements.conversionUpload.addEventListener("click", () => {
    if (elements.questionInput.value.trim()) {
      showToast("请先提交或清空当前问题，再提交转换材料");
      return;
    }
    elements.conversionFile.click();
  });
  elements.conversionFile.addEventListener("change", async () => {
    const file = elements.conversionFile.files?.[0];
    elements.conversionFile.value = "";
    if (!file || state.loading || !state.sessionReady) return;
    const session = activeSession();
    const nonce = ++conversionReadNonce;
    conversionReading = true;
    elements.conversionInputStatus.textContent = "正在读取转换材料";
    elements.conversionInputStatus.hidden = false;
    updateComposerState();
    try {
      if (!file.name.toLowerCase().endsWith(".json") || file.size > MAX_CONVERSION_BYTES) throw new Error("invalid_file");
      const raw = await file.text();
      const parsed = JSON.parse(raw);
      if (!parsed || Array.isArray(parsed) || Object.keys(parsed).sort().join(",") !== "bindings,sql,table_mapping") throw new Error("invalid_shape");
      if (nonce !== conversionReadNonce || activeSession() !== session || !state.sessionReady) return;
      conversionReading = false;
      elements.conversionInputStatus.hidden = true;
      if (elements.questionInput.value.trim()) {
        showToast("转换材料未提交，请先处理当前问题");
        return;
      }
      // Keep the original JSON string: parsing/stringifying it would round decimals.
      await sendQuestion(CONVERSION_QUESTION, null, "", {session, conversionInput: raw});
    } catch {
      if (nonce !== conversionReadNonce || activeSession() !== session) return;
      elements.conversionInputStatus.textContent = "材料未提交：请选择不超过 200 KB 的完整转换材料文件。";
      elements.conversionInputStatus.hidden = false;
    } finally {
      if (nonce === conversionReadNonce) {
        conversionReading = false;
        updateComposerState();
      }
    }
  });
  elements.questionInput.addEventListener("input", updateComposerState);
  elements.questionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitComposer();
    }
  });
  elements.newChatButton.addEventListener("click", startNewSession);
  elements.reportEntryButton.addEventListener("click", openReportDialog);
  elements.definitionButton.addEventListener("click", openDefinitionDialog);
  elements.learningButton.addEventListener("click", openLearningDialog);
  elements.definitionClose.addEventListener("click", closeDefinitionDialog);
  elements.definitionCancel.addEventListener("click", closeDefinitionDialog);
  elements.definitionForm.addEventListener("submit", submitDefinition);
  elements.definitionDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDefinitionDialog(); });
  elements.reportClose.addEventListener("click", closeReportDialog);
  elements.reportCancel.addEventListener("click", closeReportDialog);
  elements.reportForm.addEventListener("submit", submitReportDialog);
  elements.reportDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeReportDialog(); });
  elements.reportSourceSelect.addEventListener("change", () => {
    elements.reportDialogStatus.hidden = true;
    renderReportDialogState();
  });
  elements.reportQueryButton.addEventListener("click", () => {
    closeReportDialog();
    elements.questionInput.focus();
  });
  elements.queryFeedbackClose.addEventListener("click", closeQueryFeedback);
  elements.queryFeedbackCancel.addEventListener("click", closeQueryFeedback);
  elements.queryFeedbackForm.addEventListener("submit", submitQueryFeedbackForm);
  elements.queryFeedbackDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeQueryFeedback(); });
  elements.learningClose.addEventListener("click", closeLearningDialog);
  elements.learningDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeLearningDialog(); });
  elements.clearHistoryButton.addEventListener("click", async () => {
    if (state.loading) return;
    if (hasAnyPendingClarification()) {
      showToast("请先完成当前口径选择");
      return;
    }
    const session = activeSession();
    if (session.events.some((event) => event.kind === "proposal" || event.kind === "interpretation")) {
      // Closing the display binding invalidates pending/late assessments. Use
      // a new display id without replacing the canonical backend conversation.
      if (!await proposalReview.closeSession(session)) return;
      if (!state.sessions.includes(session)) return;
      const wasActive = state.activeId === session.id;
      session.id = randomId();
      if (wasActive) {
        state.activeId = session.id;
        clearTransientInputs();
      }
    }
    session.events = [];
    session.pendingClarification = null;
    resetInspector();
    render();
    updateComposerState();
    showToast("已清空当前显示，共享上下文未删除");
  });
  elements.sidebarToggle.addEventListener("click", openSidebar);
  elements.mobileBackdrop.addEventListener("click", () => { closeSidebar(); toggleInspector(false); });
  elements.inspectorToggle.addEventListener("click", () => toggleInspector());
  elements.inspectorClose.addEventListener("click", () => toggleInspector(false));
  elements.themeToggle.addEventListener("click", () => applyTheme(elements.body.dataset.theme === "dark" ? "light" : "dark"));
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
      event.preventDefault();
      startNewSession();
    }
    if (event.key === "Escape") {
      closeSidebar();
      toggleInspector(false);
      if (elements.definitionDialog.open) closeDefinitionDialog();
      if (elements.queryFeedbackDialog.open) closeQueryFeedback();
      if (elements.learningDialog.open) closeLearningDialog();
    }
  });

  initTheme();
  updateComposerState();
  render();
  refreshHealth();
  bootstrapSession();
  window.setTimeout(loadRuleCatalog, 0);
  window.setInterval(refreshHealth, 20000);
})();
