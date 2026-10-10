async (page) => {
  await page.unroute("**/v1/**");
  await page.route("**/v1/**", async (route) => {
    const endpoint = route.request().url().split("?")[0];
    const conversation = "c_guidance_fixture";
    let body = {};
    if (endpoint.endsWith("/v1/session")) {
      body = { status: "ready", identity_mode: "installation", conversation_id: conversation,
        csrf_token: "fixture_csrf_token_1234567890", clarification_pending: false,
        context_state: { scope: "principal", has_context: true, status: "ready" } };
    } else if (endpoint.endsWith("/v1/conversations")) {
      body = { conversation_id: conversation };
    } else if (endpoint.endsWith("/v1/tasks")) {
      const unsafe = route.request().postDataJSON().question.includes("不安全");
      body = { task_id: "task_00000000000000000000000000000002", state: "completed", stage: "completed", http_status: 200,
        result: { request_id: "guidance_fixture_request", conversation_id: conversation,
          status: "completed", result_state: "explanation", outcome: { state: "completed" },
          process: { state: "not_queried", steps: [] }, clarification: { required: false, questions: [] },
          guidance: { kind: "query_outcomes", format: "plain_text", title: "查询状态说明",
            text: unsafe ? "SELECT PRIVATE_DIAGNOSTIC FROM private_table" :
              "后端拒绝：查询被后端限制拒绝，未执行取数。先核对查询范围。\n\n执行失败：查询执行失败，未返回可确认结果。修正原因后重试。\n\n成功无数据：查询已成功执行，当前范围没有匹配记录，不应编造为零。\n\n结果截断：不能作为完整结果；按月分段查询并核对合计。" },
          business_result: { format: "plain_text", text: "不应展示的伪造数据" }
        } };
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
}
