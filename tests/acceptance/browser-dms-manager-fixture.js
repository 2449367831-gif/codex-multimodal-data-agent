async (page) => {
  await page.unroute("**/v1/**");
  const conversation = "c_dms_manager_fixture";
  const resultId = `rp_${"M".repeat(32)}`;
  const expires = Math.floor(Date.now() / 1000) + 3600;
  const states = ["已匹配", "未配置经理", "经理标识无效", "未找到经理", "经理记录已删除", "经理姓名缺失", "经理资料冲突"];
  const rows = Array.from({length: 105}, (_, index) => ({
    order_date: "2026-09-09", province: index === 1 ? null : "测试省区",
    manager_key: index === 1 ? null : index === 2 ? "TEST-INVALID" : "9223372036854775807",
    manager_name: index > 0 && index < 7 ? null : "测试经理",
    manager_state: states[index < 7 ? index : 0],
    customer_code: index === 1 ? null : String(index).padStart(22, "0"),
    customer_name: index === 1 ? null : `测试客户${index}长名称用于验证完整阅读与换行`,
    order_count: index < 100 ? "2" : "1", missing_manager_order_count: index > 0 && index < 7 ? "2" : "0"
  }));
  const columns = [
    ["order_date", "创建日期"], ["province", "省区"], ["manager_key", "经理标识"],
    ["manager_name", "城市经理"], ["manager_state", "经理资料状态"],
    ["customer_code", "客户编码"], ["customer_name", "客户名称"],
    ["order_count", "订单数"], ["missing_manager_order_count", "经理缺失订单数"]
  ].map(([key, label]) => ({key, label, kind: key.endsWith("order_count") ? "number" : "text",
    align: key.endsWith("order_count") ? "right" : "left", ...(key.endsWith("order_count") ? {unit: "单"} : {})}));
  const table = (number, empty = false) => ({format: "table", title: "DMS城市经理订单统计",
    caption: "全部创建日期 · 按订单创建时间 · 北京时间", empty_label: "查询成功，当前范围没有匹配的DMS销售订单",
    columns, rows: empty ? [] : rows.slice((number - 1) * 100, number * 100),
    summary: [{label: "订单数", value: empty ? "0" : "205", unit: "单"}, {label: "城市经理缺失订单数", value: empty ? "0" : "12", unit: "单"}],
    notes: ["订单号去重后分组；省区和客户名称取订单记录，经理姓名取当前员工资料。", "经理缺失或异常订单仍计入总数，不根据名称合并经理。", "修改时间不是同步时间，两次一致读取不代表原子快照。"],
    ...(empty ? {} : {pagination: {result_id: resultId, page: number, page_count: 2, total_rows: 105,
      start_row: number === 1 ? 1 : 101, end_row: number === 1 ? 100 : 105, expires_at: expires, reconciled: true}})});
  let mode = "success";
  let pendingRelease = null;
  let gets = 0;
  await page.route("**/v1/**", async (route) => {
    const endpoint = route.request().url().split("?")[0];
    let body = {};
    if (endpoint.endsWith("/v1/fixture")) {
      if (route.request().method() === "POST") {
        mode = route.request().postDataJSON().mode;
        if (mode !== "pending" && pendingRelease) { pendingRelease(); pendingRelease = null; }
      }
      body = {mode, gets};
    } else if (endpoint.endsWith("/v1/session")) {
      body = {status: "ready", identity_mode: "installation", conversation_id: conversation,
        csrf_token: "fixture_csrf_token_1234567890", clarification_pending: false,
        context_state: {scope: "principal", has_context: true, status: "ready"}};
    } else if (endpoint.endsWith("/v1/conversations")) body = {conversation_id: conversation};
    else if (endpoint.includes("/v1/results/")) {
      gets += 1;
      if (mode === "pending") await new Promise((resolve) => { pendingRelease = resolve; });
      if (mode === "expired") {
        await route.fulfill({status: 410, contentType: "application/json", body: JSON.stringify({error: {code: "result_page_unavailable", message: "结果已过期，请重新查询"}})});
        return;
      }
      const next = table(Number(endpoint.split("/").pop()));
      if (mode === "changed-summary") next.summary = [{label: "订单数", value: "999", unit: "单"}];
      if (mode === "changed-caption") next.caption = "仅2026年8月";
      if (mode === "changed-notes") next.notes = ["仅包含已付款订单"];
      if (mode === "changed-expiry") next.pagination.expires_at += 1000;
      if (mode === "unsafe-row") next.rows[0] = {...next.rows[0], customer_name: "SELECT PRIVATE_DIAGNOSTIC FROM secrets"};
      if (mode === "unknown-column") next.columns = [...next.columns, {key: "password", label: "客户名称"}];
      body = {business_table: next};
    } else if (endpoint.endsWith("/v1/tasks")) {
      body = {task_id: "task_00000000000000000000000000000026", state: "completed", http_status: 200,
        result: {request_id: "dms_manager_fixture", conversation_id: conversation, status: "completed",
          result_state: mode === "empty" ? "no_data" : "live_success", outcome: {state: "completed"},
          clarification: {required: false, questions: []}, process: {state: "completed", steps: [{id: "query", state: "completed"}]},
          business_table: table(1, mode === "empty")}};
    }
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(body)});
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
}
