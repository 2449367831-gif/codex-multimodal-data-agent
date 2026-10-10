async page => {
  let pending = null;
  const conversation = 'c_model_clarification_fixture';
  const context = {scope: 'request', has_context: false, query_context_mode: 'fresh_per_request'};
  const card = {required: true, token: 'ct_' + 'a'.repeat(32), draft_id: 'cd_' + 'b'.repeat(64), questions: [{
    id: 'two_c_basis', prompt: '按哪个业务标识识别2C订单？', selection: 'single', options: [
      {id: 'shipment_fulfillment', label: '出库单履约为2C', available: true},
      {id: 'sales_order_channel', label: '销售订单渠道为2C', available: true}]}]};
  await page.route('**/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body = {};
    if (path.endsWith('/session')) body = {status: 'ready', identity_mode: 'installation',
      conversation_id: conversation, csrf_token: 'fixture_csrf_token_1234567890',
      clarification_pending: Boolean(pending), context_state: context};
    if (path.endsWith('/clarification/resume')) body = {conversation_id: conversation,
      clarification: pending || {required: false, questions: []}, context_state: context};
    if (path.endsWith('/conversations')) body = {conversation_id: conversation};
    if (path.endsWith('/tasks')) {
      const submission = route.request().postDataJSON();
      const selected = submission.clarification_answers;
      if (selected && (selected[0]?.question_id !== 'two_c_basis' ||
          selected[0]?.option_ids?.[0] !== 'shipment_fulfillment')) {
        await route.fulfill({status: 422, contentType: 'application/json', body: JSON.stringify({
          error: {code: 'clarification_invalid', message: '测试检测到选项标识被改写'}
        })});
        return;
      }
      pending = selected ? null : card;
      const result = {conversation_id: conversation, context_state: context,
        status: selected ? 'completed' : 'needs_selection',
        query_status: selected ? 'succeeded' : 'clarification_required',
        outcome: {state: selected ? 'completed' : 'clarification_required'},
        result_state: selected ? 'live_success' : 'clarification_required',
        clarification: pending || {required: false, questions: []},
        process: {steps: selected ? [{id: 'query', state: 'completed'}, {id: 'validation', state: 'completed'}]
          : [{id: 'context', state: 'fresh'}, {id: 'query', state: 'pending'}, {id: 'compaction', state: 'not_needed'}]}};
      if (selected) result.business_table = {format: 'table', title: '合成测试结果',
        caption: '仅用于界面验收，不是生产数据', columns: [
          {key: 'col_1', label: 'SKU名称', kind: 'text'},
          {key: 'col_2', label: '实发数量', kind: 'number', unit: 'EA'}],
        rows: [{col_1: '测试商品', col_2: '12'}], summary: [], empty_label: '当前无数据'};
      body = {task_id: 'task_' + (selected ? '2' : '1').repeat(32), state: 'completed',
        http_status: 200, result};
    }
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
}
