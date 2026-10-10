async (page) => {
  await page.unroute("**/v1/**");
  const conversation = "c_conversion_fixture";
  const tasks = new Map();
  let sequence = 0;
  await page.route("**/v1/**", async (route) => {
    const endpoint = route.request().url().split("?")[0];
    let body = {};
    if (endpoint.endsWith("/v1/session")) {
      body = {status: "ready", identity_mode: "installation", conversation_id: conversation,
        csrf_token: "fixture_csrf_token_1234567890", clarification_pending: false,
        context_state: {scope: "principal", has_context: true, status: "ready"}};
    } else if (endpoint.endsWith("/v1/conversations")) {
      body = {conversation_id: conversation};
    } else if (endpoint.endsWith("/v1/tasks")) {
      const request = route.request().postDataJSON();
      const state = request.sql_conversion ? (request.sql_conversion.includes("UNKNOWN") ? "rejected" : "converted") : "needs_input";
      const unsafe = request.question.includes("不安全");
      const text = {
        converted: "转换检查已完成，未查询数据。已处理日期与条件表达式、参数绑定和明确的库表映射，并校验字段归属与别名。业务口径尚需独立对账，不能据此确认业务结果。",
        needs_input: "尚未转换。缺少待转换的原始查询、明确的库表对应关系及参数值；无参数时也需明确为空。未查询数据。",
        rejected: "转换未通过，未查询数据。请核对原始查询、库表对应关系、字段和参数。"
      }[state];
      const id = `task_${(++sequence).toString(16).padStart(32, "0")}`;
      const result = {request_id: `conversion_${sequence}`, conversation_id: conversation, status: "completed",
        result_state: "explanation", outcome: {state: "completed"}, process: {state: "not_queried", steps: []},
        clarification: {required: false, questions: []}, guidance: {kind: "sql_conversion", title: "查询转换检查",
          format: "plain_text", state, text: unsafe ? "SELECT PRIVATE_RESPONSE FROM secret_table" : text},
        business_result: {format: "plain_text", text: "不应出现的伪造业务结果"}};
      tasks.set(id, {task_id: id, state: "completed", http_status: 200, result});
      body = {task_id: id, state: "queued", stage: "routing", http_status: 200};
      await page.evaluate((flags) => { window.conversionFixtureFlags = flags; }, {
        requestCount: sequence, originalDecimal: !request.sql_conversion || request.sql_conversion.includes("0.1234567890123456789"),
        privateField: typeof request.sql_conversion === "string", questionSafe: !request.question.includes("PRIVATE_INPUT")
      });
    } else if (endpoint.includes("/v1/tasks/")) {
      body = tasks.get(endpoint.split("/").pop()) || {};
    }
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(body)});
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
}
