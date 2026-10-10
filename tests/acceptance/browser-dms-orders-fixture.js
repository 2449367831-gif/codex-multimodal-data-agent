async (page) => {
  await page.unroute("**/v1/**");
  const conversation = "c_dms_orders_fixture";
  const expiresAt = Math.floor(Date.now() / 1000) + 3600;
  const monthly = { format: "table", title: "积分订单月度统计", caption: "2026年1月至8月 · 按订单创建时间 · 北京时间", empty_label: "查询成功，当前范围没有匹配的积分订单",
    columns: [{key:"month",label:"创建月份"},{key:"order_count",label:"订单数"}],
    rows: [{month:"2026-01",order_count:"2"},{month:"2026-08",order_count:"1"}],
    summary: [{label:"兑换订单数",value:"3",unit:"单"}],
    notes: ["积分订单按订单号去重，排除逻辑删除；未附加付款、出库完成或商品条件。", "客户编码缺失 1 单，客户名称缺失 1 单；缺失订单仍计入总数。", "修改时间不代表同步时间，历史同步完整性尚未确认。"] };
  const detail = (number) => ({format:"table",title:"积分订单明细",caption:monthly.caption,empty_label:monthly.empty_label,
    columns: [{key:"sales_order_code",label:"订单号"},{key:"customer_code",label:"客户编码"},{key:"customer_name",label:"客户名称"},{key:"created_time",label:"创建时间"}],
    rows: number===1 ? [{sales_order_code:"TEST-ORDER-001",customer_code:"000012345678901234567890",customer_name:"测试客户甲",created_time:"2026-01-01 00:00:00"},
      {sales_order_code:"TEST-ORDER-002",customer_code:null,customer_name:null,created_time:"2026-01-31 23:59:59"}]
      : [{sales_order_code:"TEST-ORDER-003",customer_code:"0003",customer_name:"测试客户乙长名称用于验证可读性与单元格换行",created_time:"2026-08-31 23:59:59"}],
    summary:[],notes:["订单粒度；客户名称取订单记录，客户编码保留前导零。月度汇总已与完整订单明细逐月对账。"],
    pagination:{result_id:`rp_${"D".repeat(32)}`,page:number,page_count:2,total_rows:3,start_row:number===1?1:3,end_row:number===1?2:3,expires_at:expiresAt,reconciled:true}});
  await page.route("**/v1/**",async (route) => {
    const endpoint=route.request().url().split("?")[0];
    let body={};
    const match=endpoint.match(/\/v1\/results\/rp_D{32}\/pages\/(\d+)$/);
    if(endpoint.endsWith('/v1/session')) body={status:'ready',identity_mode:'installation',conversation_id:conversation,csrf_token:'fixture_csrf_token_1234567890',clarification_pending:false,context_state:{scope:'principal',has_context:true,status:'ready'}};
    else if(endpoint.endsWith('/v1/conversations')) body={conversation_id:conversation};
    else if(match){
      const flags=await page.evaluate(()=>({failure:window.dmsPageFailure}));
      if(flags.failure){await route.fulfill({status:410,contentType:'application/json',body:JSON.stringify({error:{code:'result_page_unavailable',message:'结果已过期，请重新查询'}})});return;}
      body={business_table:detail(Number(match[1]))};
    } else if(endpoint.endsWith('/v1/tasks')){
      const flags=await page.evaluate(()=>({invalid:window.dmsInvalidTable}));
      const tables=[monthly,detail(1)];
      if(flags.invalid) tables[1].rows[0].customer_name='<script>unsafe</script>';
      body={task_id:'task_00000000000000000000000000000025',state:'completed',stage:'completed',http_status:200,
        result:{request_id:'dms_orders_fixture',conversation_id:conversation,status:'completed',result_state:'live_success',outcome:{state:'completed'},
          clarification:{required:false,questions:[]},process:{state:'completed',steps:[{id:'query',state:'completed'}]},
          business_result:{format:'plain_text',text:'积分订单共3单，月度统计与明细已对账。'},business_table:monthly,business_tables:tables}};
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
