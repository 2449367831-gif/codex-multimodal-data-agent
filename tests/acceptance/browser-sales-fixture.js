async (page) => {
  await page.unroute("**/v1/**");
  await page.route("**/v1/**", async (route) => {
    const pathname = route.request().url().split("?")[0].replace(/^https?:\/\/[^/]+/, "");
    let body = {};
    if (pathname === "/v1/session") body = {
      status: "ok", identity_mode: "installation", conversation_id: "c_sales_fixture",
      csrf_token: "fixture_csrf_token_1234567890", clarification_pending: false,
      context_state: { scope: "principal", has_context: true, status: "ready" }
    };
    if (pathname === "/v1/conversations") body = { conversation_id: "c_sales_fixture" };
    if (pathname === "/v1/learning/summary") body = { status: "ok" };
    if (pathname === "/v1/tasks") body = {
      task_id: "task_00000000000000000000000000000002", state: "completed", stage: "completed", http_status: 200,
      result: {
        request_id: "sales_fixture", conversation_id: "c_sales_fixture", status: "completed", result_state: "live_success",
        outcome: { state: "completed" }, clarification: { required: false, questions: [] },
        business_table: {
          format: "table", title: "销售出库数量与单数", caption: "测试自然日 · 全部货主 · 按仓汇总",
          empty_label: "当前范围没有有效销售出库记录",
          notes: ["零数量的有效出库单仍计入单数；出库单数不等于销售订单数。"],
          columns: [
            { key: "cargo_owner", label: "货主" }, { key: "warehouse_code", label: "仓库编码" },
            { key: "warehouse_name", label: "仓库名称" }, { key: "quantity", label: "实际销售出库" },
            { key: "outbound_count", label: "出库单数" }
          ],
          rows: Array.from({ length: 120 }, (_, i) => ({
            cargo_owner: "测试货主", warehouse_code: "TEST" + String(i + 1).padStart(3, "0"),
            warehouse_name: "测试仓库" + (i + 1), quantity: i === 0 ? "0" : "1.25", outbound_count: "1"
          })),
          summary: [{ label: "实际销售出库", value: "148.75", unit: "EA" }, { label: "出库单数", value: "120", unit: "单" }]
        }
      }
    };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("http://127.0.0.1:8021/");
}
