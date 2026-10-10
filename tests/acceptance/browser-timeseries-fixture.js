async (page) => {
  await page.context().clearCookies();
  await page.goto("about:blank");
  await page.unroute("**/v1/**");
  await page.route("**/v1/**", async (route) => {
    const pathname = route.request().url().split("?")[0].replace(/^https?:\/\/[^/]+/, "");
    let body = {};
    if (pathname === "/v1/session") body = {
      status: "ok", identity_mode: "installation", conversation_id: "c_timeseries_fixture",
      csrf_token: "fixture_csrf_token_1234567890", clarification_pending: false,
      context_state: { scope: "principal", has_context: true, status: "ready" }
    };
    if (pathname === "/v1/conversations") body = { conversation_id: "c_timeseries_fixture" };
    if (pathname === "/v1/learning/summary") body = { status: "ok" };
    if (pathname === "/v1/tasks" || pathname.startsWith("/v1/tasks/")) body = {
      task_id: "task_00000000000000000000000000000014", state: "completed", stage: "completed", http_status: 200,
      result: {
        request_id: "timeseries_fixture", conversation_id: "c_timeseries_fixture", status: "completed", result_state: "live_success",
        outcome: { state: "completed" }, clarification: { required: false, questions: [] },
        business_table: {
          table_id: "sales_timeseries", format: "table", title: "销售出库日历与窗口分析",
          caption: "合成测试 · 14个自然日 · 全部货主",
          empty_label: "当前范围没有有效销售出库记录",
          notes: ["累计从区间起点开始；5日窗口包含当日，不足完整窗口为空。"],
          columns: [
            { key: "ship_date", label: "日期", kind: "text" },
            { key: "quantity", label: "实发数量", kind: "number", unit: "EA" },
            { key: "outbound_count", label: "出库单数", kind: "number", unit: "单" },
            { key: "cumulative_quantity", label: "累计实发数量", kind: "number", unit: "EA" },
            { key: "moving_average", label: "5日移动平均", kind: "number", unit: "EA/日", nullable: true }
          ],
          rows: Array.from({ length: 14 }, (_, i) => ({
            ship_date: `2026-09-${String(i + 1).padStart(2, "0")}`,
            quantity: "10", outbound_count: "2", cumulative_quantity: String((i + 1) * 10),
            moving_average: i < 4 ? null : "10.00"
          })),
          summary: [{ label: "实际销售出库", value: "140", unit: "EA" }]
        }
      }
    };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("http://127.0.0.1:8021/");
}
