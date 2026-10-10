async (page) => {
  const conversationId = "c_browser_fixture";
  await page.unroute("**/v1/**");
  await page.route("**/v1/**", async (route) => {
    const pathname = await page.evaluate((url) => new URL(url).pathname, route.request().url());
    let body = {};
    if (pathname === "/v1/session") {
      body = {
        status: "ok", identity_mode: "installation", conversation_id: conversationId,
        csrf_token: "fixture_csrf_token_1234567890",
        context_state: { scope: "principal", has_context: true, status: "ready" },
        clarification_pending: false
      };
    } else if (pathname === "/v1/conversations") {
      body = { conversation_id: conversationId };
    } else if (pathname === "/v1/learning/summary") {
      body = { status: "ok" };
    } else if (pathname === "/v1/tasks") {
      const question = route.request().postDataJSON().question || "";
      const empty = question.includes("不存在");
      const truncated = question.includes("截断");
      body = {
        task_id: "task_00000000000000000000000000000001",
        state: "completed", stage: "completed", http_status: truncated ? 409 : 200,
        result: truncated ? {
          error: { code: "query_truncated", result_state: "truncated", message: "PRIVATE_DIAGNOSTIC" }
        } : {
          request_id: "fixture_request", conversation_id: conversationId,
          status: "completed", result_state: empty ? "no_data" : "live_success",
          outcome: { state: "completed" }, clarification: { required: false, questions: [] },
          business_result: { format: "plain_text", text: empty ? "查询成功，当前范围没有匹配的仓库资料" : "查询成功，以下为测试仓库资料" },
          business_table: {
            format: "table", table_id: "warehouse_directory", title: "仓库资料",
            caption: "测试名称范围", empty_label: "查询成功，当前范围没有匹配的仓库资料",
            columns: [
              { key: "cargo_owner", label: "货主" },
              { key: "warehouse_code", label: "仓库编码" },
              { key: "warehouse_name", label: "仓库名称" },
              { key: "warehouse_type", label: "仓库类型" },
              { key: "warehouse_node", label: "仓库节点" }
            ],
            rows: empty ? [] : [
              { cargo_owner: "测试货主甲", warehouse_code: "TEST001", warehouse_name: "测试沈阳一仓", warehouse_type: "云仓（12）", warehouse_node: "经营中心（10）" },
              { cargo_owner: "测试货主乙", warehouse_code: "TEST002", warehouse_name: "测试沈阳二仓", warehouse_type: "未映射（99）", warehouse_node: "未配置" }
            ],
            summary: [{ label: "仓库数", value: empty ? "0" : "2", unit: "个" }]
          }
        }
      };
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("http://127.0.0.1:8021/");
}
