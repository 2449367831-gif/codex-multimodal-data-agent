async page => {
  let pending = null;
  const groups = [
    ['tc_variant', '请选择本次讨论的历史方案；选择不代表口径已发布。', [['tc_a', 'A：当前实时可用库存 / 近7天日均销售出库'], ['tc_b', 'B：月初库存与本月累计出库（公式待确认）']]],
    ['tc_stock', '库存采用哪个时点？', [['tc_realtime', '当前实时可用库存（不含在途）'], ['tc_month_start', '本月月初历史库存快照']]],
    ['tc_sales', '销售数量采用哪个业务口径？', [['tc_actual', '实际完成的销售出库数量'], ['tc_allocated', '分配数量（单位关系未确认）']]],
    ['tc_returns', '销售数量是否扣减退货？', [['tc_keep_returns', '不扣减退货，按出库量统计'], ['tc_deduct_returns', '扣减退货（退货口径待确认）']]],
    ['tc_scope', '货主、仓库范围采用哪一种？', [['tc_all', '全部货主、全部仓库；其余未说明范围均为全部'], ['tc_specified', '指定范围（未明确的范围暂不执行）']]],
    ['tc_window', '销售日均量采用哪个时间区间？', [['tc_7days', '最近7个完整自然日'], ['tc_mtd', '本自然月1日至提问当天00:00'], ['tc_custom', '指定日期区间']]]
  ];
  await page.route('**/v1/**', async route => {
    const path = route.request().url().split('?')[0];
    let body = {};
    if (path.endsWith('/session')) body = {status:'ready', identity_mode:'installation', conversation_id:'c_conflict_fixture', csrf_token:'fixture_csrf_token_1234567890', clarification_pending:Boolean(pending), context_state:{scope:'principal',has_context:true,status:'ready'}};
    if (path.endsWith('/clarification/resume')) body = {conversation_id:'c_conflict_fixture', clarification:pending || {required:false,questions:[]}};
    if (path.endsWith('/conversations')) body = {conversation_id:'c_conflict_fixture'};
    if (path.endsWith('/tasks')) {
      const submitted = route.request().postDataJSON();
      const selected = submitted.clarification_answers;
      if (selected) {
        const window = selected.find(answer => answer.question_id === 'tc_window');
        if (selected.length !== 6 || window.option_ids[0] !== 'tc_custom' || window.date_range?.start !== '2026-08-01' || window.date_range?.end !== '2026-09-01') {
          await route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({error:{code:'clarification_invalid'}})});
          return;
        }
      }
      const result = !selected ? {
        status:'needs_selection', result_state:'clarification_required', conversation_id:'c_conflict_fixture', outcome:{state:'clarification_required'},
        clarification:{required:true,token:'ct_conflict_fixture_selection_1234567890',draft_id:'cd_'+'1'.repeat(64),questions:groups.map(([id,prompt,options])=>({id,prompt,selection:'single',options:options.map(([id,label])=>({id,label})),...(id === 'tc_window' ? {date_range_option:'tc_custom'} : {})}))}
      } : {
        status:'blocked', result_state:'business_definition_unpublished', conversation_id:'c_conflict_fixture', outcome:{state:'blocked'}, clarification:{required:false,questions:[]},
        message:'已保留本次选择。历史方案尚未发布，不能替代已确认的成品库存加在途口径；销售区间与方案不一致。本次未执行查询，无需重复选择。'
      };
      pending = selected ? null : result.clarification;
      body = {task_id:selected?'task_00000000000000000000000000000092':'task_00000000000000000000000000000091', state:'completed', http_status:200, result};
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
