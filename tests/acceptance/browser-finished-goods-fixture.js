async (page) => {
  await page.unroute("**/v1/**");
  await page.route("**/v1/**", async (route) => {
    const path = route.request().url().split("?")[0];
    const conversation = "c_finished_fixture";
    let body = {};
    if (path.endsWith("/v1/session")) body = {
      status: "ready", identity_mode: "installation", conversation_id: conversation,
      csrf_token: "fixture_csrf_token_1234567890", clarification_pending: false,
      context_state: { scope: "principal", has_context: true, status: "ready" }
    };
    if (path.endsWith("/v1/conversations")) body = { conversation_id: conversation };
    if (path.endsWith("/v1/tasks")) body = {
      task_id: "task_00000000000000000000000000000007", state: "completed", stage: "completed", http_status: 200,
      result: {
        request_id: "finished_fixture", conversation_id: conversation, status: "completed", result_state: "live_success",
        outcome: { state: "completed" }, clarification: { required: false, questions: [] },
        business_table: {
          format: "table", title: "销售出库数量与单数", caption: "测试自然日 · 全部货主 · 成品（含指定例外）",
          empty_label: "当前范围没有有效销售出库记录",
          notes: ["零数量的有效出库单仍计入单数；出库单数不等于销售订单数。", "最近出库时间：2026-09-09 12:00:00。这是业务事件时间，不代表数据同步时间。", "成品范围为成品组50，另保留例外SKU 5101010010008、5101010010009；普通辅料不计入。"],
          columns: [{ key: "sku_code", label: "SKU", kind: "text" }, { key: "quantity", label: "实际销售出库", kind: "number", unit: "EA" }, { key: "outbound_count", label: "出库单数", kind: "number", unit: "单" }],
          rows: [{ sku_code: "TEST-SKU-A", quantity: "13.75", outbound_count: "4" }, { sku_code: "5101010010008", quantity: "2", outbound_count: "1" }, { sku_code: "5101010010009", quantity: "3", outbound_count: "1" }],
          summary: [{ label: "实际销售出库", value: "18.75", unit: "EA" }, { label: "出库单数", value: "4", unit: "单" }]
        }
      }
    };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
}
