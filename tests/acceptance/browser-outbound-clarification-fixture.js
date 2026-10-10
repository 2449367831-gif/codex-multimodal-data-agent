async page => {
  let pending = null;
  const questionGroups = [
    ['outbound_business', '请选择出库业务。', [
      ['outbound_sales', '销售出库'], ['outbound_transfer', '调拨出库（按完成状态和实际发出数量）'],
      ['outbound_adjustment', '调整出库（暂未接入）'], ['outbound_other', '其他出库（暂未接入）'], ['outbound_all', '所有出库（销售与调拨）']]],
    ['outbound_measure', '请选择统计项；实发、分配、请求数量与单数不能互相代替。', [
      ['outbound_actual_qty', '实发数量'], ['outbound_allocated_qty', '分配数量（单位待确认）'],
      ['outbound_requested_qty', '请求数量（单位待确认）'], ['outbound_document_count', '出库单数（此组合暂未接入）'], ['outbound_order_count', '销售订单数（暂未接入）']]],
    ['outbound_time_axis', '昨天按哪个业务时间统计？', [
      ['outbound_shipped_at', '实际出库时间'], ['outbound_order_created_at', '订单创建时间（暂未接入）'], ['outbound_reviewed_at', '审核时间（暂未接入）']]]
  ];
  await page.route('**/v1/**', async route => {
    const path = route.request().url().split('?')[0];
    let body = {};
    if (path.endsWith('/session')) body = {status:'ready',identity_mode:'installation',conversation_id:'c_outbound_fixture',csrf_token:'fixture_csrf_token_1234567890',clarification_pending:Boolean(pending),context_state:{scope:'principal',has_context:true,status:'ready'}};
    if (path.endsWith('/clarification/resume')) body = {conversation_id:'c_outbound_fixture',clarification:pending || {required:false,questions:[]}};
    if (path.endsWith('/conversations')) body = {conversation_id:'c_outbound_fixture'};
    if (path.endsWith('/tasks')) {
      const submitted = route.request().postDataJSON();
      const selected = submitted.clarification_answers;
      const runnable = selected?.every(answer => ['outbound_sales','outbound_actual_qty','outbound_shipped_at'].includes(answer.option_id || answer.option_ids?.[0]));
      const result = !selected ? {
        status:'needs_selection',result_state:'clarification_required',conversation_id:'c_outbound_fixture',outcome:{state:'clarification_required'},
        clarification:{required:true,token:'ct_outbound_fixture_selection_1234567890',questions:questionGroups.map(([id,prompt,options])=>({id,prompt,selection:'single',options:options.map(([id,label])=>({id,label}))}))}
      } : runnable ? {
        status:'completed',result_state:'live_success',conversation_id:'c_outbound_fixture',outcome:{state:'completed'},clarification:{required:false,questions:[]},
        business_table:{format:'table',title:'销售出库数量与单数',caption:'测试自然日 · 全部货主 · 全部仓库',empty_label:'查询成功，当前范围没有记录',
          columns:[{key:'quantity',label:'实际销售出库',kind:'number',unit:'EA'},{key:'outbound_count',label:'出库单数',kind:'number',unit:'单'}],
          rows:[{quantity:'18',outbound_count:'4'}],summary:[],notes:['此页使用合成测试数据。']}
      } : {
        status:'blocked',result_state:'business_definition_unpublished',conversation_id:'c_outbound_fixture',outcome:{state:'blocked'},clarification:{required:false,questions:[]},
        message:'已保留本次选择。所选出库业务尚未接入已核验的查询；所选数量的单位口径尚未确认，不能用实发数量代替；所选业务时间尚未接入此查询组合，不能用实际出库时间代替。本次未执行查询，无需重复选择。'
      };
      pending = selected ? null : result.clarification;
      body = {task_id: selected?'task_00000000000000000000000000000082':'task_00000000000000000000000000000081',state:'completed',http_status:200,result};
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
