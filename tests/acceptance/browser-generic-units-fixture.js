async page => {
  await page.emulateMedia({reducedMotion: "reduce"});
  await page.route("**/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const context_state = {scope: "principal", has_context: true, status: "ready"};
    let body = {};
    if (path === "/v1/session") body = {status: "ready", identity_mode: "installation",
      conversation_id: "c_generic_units_fixture", csrf_token: "fixture_csrf_token_1234567890", context_state};
    if (path === "/v1/conversations") body = {conversation_id: "c_generic_units_fixture"};
    if (path === "/v1/tasks") body = {
      task_id: "task_00000000000000000000000000000009", state: "completed", http_status: 200,
      result: {status: "completed", result_state: "live_success", outcome: {state: "completed"}, context_state,
        conversation_id: "c_generic_units_fixture", clarification: {required: false, questions: []},
        business_table: {format: "table", table_id: "model_verified", title: "合成计量结果", caption: "",
          columns: [
            {key: "col_1", label: "商品编码", kind: "text"},
            {key: "col_2", label: "日均数量", kind: "number", unit: "EA/日"},
            {key: "col_3", label: "金额", kind: "number", unit: "万元"},
            {key: "col_4", label: "变化率", kind: "number", unit: "%"},
          ],
          rows: [
            {col_1: "001234567890123456789", col_2: "-12345678901234567890.00100", col_3: "12.3400", col_4: "-0.0100"},
            {col_1: "000002", col_2: null, col_3: "0.0000", col_4: null},
          ], summary: []},
      },
    };
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(body)});
  });
  await page.goto("http://127.0.0.1:8021/");
}
