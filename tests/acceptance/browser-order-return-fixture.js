async (page) => {
  await page.unroute('**/v1/**');
  const conversation='c_order_return_fixture';
  const expiresAt=Math.floor(Date.now()/1000)+3600;
  const columns=(pairs)=>pairs.map(([key,label])=>({key,label}));
  const orders={format:'table',title:'退货分析订单范围',caption:'测试订单范围 · 北京时间',empty_label:'没有匹配的销售订单',
    columns:columns([['cargo_owner','货主'],['sales_order_code','销售订单'],['order_created','订单创建时间'],['order_detail_state','明细状态']]),
    rows:[{cargo_owner:'TEST',sales_order_code:'000012345678901234567890',order_created:'2026-08-01 00:00:00',order_detail_state:'有有效明细'},
      {cargo_owner:'TEST',sales_order_code:'TEST-EMPTY',order_created:'2026-08-02 00:00:00',order_detail_state:'无有效销售明细'}],
    summary:[{label:'订单数',value:'2',unit:'单'}],notes:[]};
  const quantity=(number)=>({format:'table',title:'订单退货数量核对',caption:orders.caption,empty_label:'没有可展示的商品数量',
    columns:columns([['cargo_owner','货主'],['sales_order_code','销售订单'],['sku_code','SKU'],['order_ship_quantity','订单实发原始数量'],
      ['returned_quantity','退货原始数量'],['net_sales_quantity','净销售原始数量'],['return_document_count','关联退货单数'],
      ['sales_detail_count','销售明细行数'],['return_detail_count','退货明细行数'],['quantity_state','数量状态']]),
    rows:number===1 ? [{cargo_owner:'TEST',sales_order_code:'000012345678901234567890',sku_code:'TEST-SKU-001',order_ship_quantity:'10',
      returned_quantity:'12',net_sales_quantity:'-2',return_document_count:'1',sales_detail_count:'2',return_detail_count:'1',quantity_state:'退货超过实发'}]
      :[{cargo_owner:'TEST',sales_order_code:'000012345678901234567890',sku_code:'TEST-SKU-002',order_ship_quantity:null,
        returned_quantity:'0',net_sales_quantity:null,return_document_count:'1',sales_detail_count:'0',return_detail_count:'1',quantity_state:'未匹配有效销售明细'}],
    summary:[],notes:['按货主、订单和商品分别核对，不跨商品合计原始数量。','无匹配销售明细时保留缺失值，不按零数量计算净销售。'],
    pagination:{result_id:`rp_${'R'.repeat(32)}`,page:number,page_count:2,total_rows:2,start_row:number,end_row:number,expires_at:expiresAt,reconciled:true}});
  await page.route('**/v1/**',async(route)=>{
    const endpoint=route.request().url().split('?')[0];
    const match=endpoint.match(/\/v1\/results\/rp_R{32}\/pages\/(\d+)$/);
    const flags=match||endpoint.endsWith('/v1/tasks')
      ? await page.evaluate(()=>({invalid:window.returnInvalidTable,conflict:window.returnReferenceConflict,expired:window.returnPageExpired})) : {};
    let body={};
    if(endpoint.endsWith('/v1/session')) body={status:'ready',identity_mode:'installation',conversation_id:conversation,
      csrf_token:'fixture_csrf_token_1234567890',clarification_pending:false,context_state:{scope:'principal',has_context:true,status:'ready'}};
    else if(endpoint.endsWith('/v1/conversations')) body={conversation_id:conversation};
    else if(match){
      if(flags.expired){await route.fulfill({status:410,contentType:'application/json',body:JSON.stringify({error:{code:'result_page_unavailable',message:'结果已过期，请重新查询'}})});return;}
      body={business_table:quantity(Number(match[1]))};
    }else if(endpoint.endsWith('/v1/tasks')){
      const tables=[orders,quantity(1)];
      if(flags.invalid) tables[1].rows[0].return_detail_count='-1';
      body={task_id:'task_00000000000000000000000000000031',state:'completed',stage:'completed',http_status:200,
        result:{request_id:'return_fixture',conversation_id:conversation,status:'completed',result_state:'live_success',outcome:{state:'completed'},
          clarification:{required:false,questions:[]},process:{state:'completed',steps:[{id:'query',state:'completed'}]},
          business_result:{format:'plain_text',text:'数量核对完成。'},business_table:orders,business_tables:tables}};
      if(flags.conflict) body={...body,http_status:422,result:{error:{code:'semantic_query_error',message:'退货订单关联不一致，整次查询未返回数据'}}};
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
