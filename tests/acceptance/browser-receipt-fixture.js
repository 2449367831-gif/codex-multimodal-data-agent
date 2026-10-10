async (page) => {
  await page.unroute("**/v1/**");
  const paginated = await page.evaluate(() => Boolean(window.receiptPaginationTest));
  let templateTable;
  await page.route("**/v1/**", async (route) => {
    const endpoint = route.request().url().split("?")[0];
    const conversation = "c_receipt_fixture";
    let body = {};
    const pageMatch = endpoint.match(/\/v1\/results\/rp_A{32}\/pages\/(\d+)$/);
    if (endpoint.endsWith("/v1/session")) {
      body = { status: "ready", identity_mode: "installation", conversation_id: conversation,
        csrf_token: "fixture_csrf_token_1234567890", clarification_pending: false,
        context_state: { scope: "principal", has_context: true, status: "ready" } };
    } else if (endpoint.endsWith("/v1/conversations")) {
      body = { conversation_id: conversation };
    } else if (pageMatch) {
      if (await page.evaluate(() => Boolean(window.receiptPageFailure))) {
        await route.fulfill({ status: 410, contentType: "application/json", body: JSON.stringify({ error: { code: "result_page_unavailable", message: "结果已过期，请重新查询" } }) });
        return;
      }
      const number = Number(pageMatch[1]);
      const value = JSON.parse(JSON.stringify(templateTable));
      value.rows = number === 3 ? [value.rows[0]] : value.rows;
      value.rows.forEach((row, index) => row.detail_id = `TEST-PAGE-${number}-LINE-${index + 1}`);
      value.pagination = { ...value.pagination, page: number, start_row: number * 2 - 1, end_row: number === 3 ? 5 : number * 2 };
      body = { business_table: value };
    } else if (endpoint.endsWith("/v1/tasks")) {
      const fields = [
        ["cargo_owner", "货主"], ["inbound_code", "入库单"], ["detail_id", "明细行标识"], ["item_code", "行号"],
        ["warehouse_code", "仓库编码"], ["warehouse_name", "仓库名称"], ["sku_code", "SKU"], ["batch_number", "批次"],
        ["production_date", "生产日期"], ["received_quantity", "实收入库数量"], ["quantity_unit", "来源单位编码"], ["actual_arrival_date", "实际入库时间"]
      ];
      body = { task_id: "task_00000000000000000000000000000003", state: "completed", stage: "completed", http_status: 200,
        result: { request_id: "receipt_fixture_request", conversation_id: conversation,
          status: "completed", result_state: "live_success", outcome: { state: "completed" },
          process: { state: "completed", steps: [{ id: "query", state: "completed", label: "已完成数据查询" }] },
          clarification: { required: false, questions: [] },
          business_result: { format: "plain_text", text: "采购识别：入库类型为采购（10）。数量保留来源单位编码；数据同步鲜度尚未确认。" },
          business_table: { format: "table", title: "采购入库明细", caption: "生产日期 2026-09-03 至 2026-09-10 · 实际入库早于 2026-09-10", empty_label: "没有匹配的采购入库明细",
            columns: fields.map(([key, label]) => ({ key, label, kind: key === "received_quantity" ? "number" : "text" })),
            rows: [{ cargo_owner: "TEST-A", inbound_code: "TEST-RECEIPT-001", detail_id: "TEST-LINE-01", item_code: "1", warehouse_code: "TEST-W1", warehouse_name: "测试仓库甲", sku_code: "TEST-SKU-001", batch_number: "TEST-BATCH-01", production_date: "2026-09-03 00:00:00", received_quantity: "1.2345", quantity_unit: "10", actual_arrival_date: "2026-09-09 18:00:00" },
              { cargo_owner: "TEST-B", inbound_code: "TEST-RECEIPT-001", detail_id: "TEST-LINE-02", item_code: null, warehouse_code: "TEST-W2", warehouse_name: null, sku_code: "TEST-SKU-002", batch_number: null, production_date: "2026-09-04 00:00:00", received_quantity: "0", quantity_unit: "40", actual_arrival_date: "2026-09-09 22:00:00" }],
            summary: [{ label: "明细行数", value: "2", unit: "个" }],
            notes: ["采购识别：入库类型为采购（10）。生产日期区间左闭右开；今天入库的记录不计入。", "数量保留来源单位编码；同步鲜度尚未确认。"] }
        } };
      if (paginated) {
        body.result.business_table.pagination = { result_id: `rp_${"A".repeat(32)}`, page: 1, page_count: 3, total_rows: 5, start_row: 1, end_row: 2, expires_at: Math.floor(Date.now() / 1000) + 3600, reconciled: true };
        body.result.business_table.summary = [{ label: "明细行数", value: "5", unit: "个" }];
        templateTable = JSON.parse(JSON.stringify(body.result.business_table));
      }
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
}
