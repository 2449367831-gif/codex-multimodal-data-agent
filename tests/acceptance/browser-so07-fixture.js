async (page, origin) => {
  await page.unroute('**/v1/**');
  await page.unroute('**/health');
  await page.route('**/health',route=>route.fulfill({status:200,contentType:'application/json',
    body:JSON.stringify({ready:true,query_backend:{ready:true},sessions:{ready:true}})}));
  const columns = [
    ['cargo_owner','货主'],['sales_order_code','订单号'],['order_created_at','订单创建时间'],
    ['customer_code','客户编码'],['customer_name','客户名称'],['warehouse_code','仓库编码'],
    ['warehouse_name','仓库名称'],['sku_code','SKU'],['sku_name','商品'],
    ['request_quantity','原始请求数量'],['quantity_unit_reference','源数量单位代码'],
    ['sales_unit_reference','源销售单位代码'],['order_status','订单状态'],['order_detail_state','明细状态']
  ].map(([key,label])=>({key,label}));
  const conversation = 'c_so07_synthetic_fixture';
  const expires = Math.floor(Date.now()/1000)+3600;
  const table = number => ({format:'table',title:'SO07社团销售订单',caption:'模拟订单创建日期 · 北京时间',
    empty_label:'当前范围没有匹配的SO07订单',columns,
    rows:[{cargo_owner:'TEST',sales_order_code:number === 1 ? '000012345678901234567890' : 'TEST-EMPTY',
      order_created_at:'2026-09-10 00:00:00',customer_code:'000003',customer_name:'模拟客户',
      warehouse_code:'W1',warehouse_name:'模拟仓库',sku_code:number === 1 ? '0001' : null,
      sku_name:number === 1 ? '模拟商品' : null,
      request_quantity:number === 1 ? '123456789012345678901234567890.000001' : null,
      quantity_unit_reference:number === 1 ? '010' : null,sales_unit_reference:number === 1 ? '040' : null,
      order_status:'0',order_detail_state:number === 1 ? '存在范围内有效明细' : '缺少范围内有效明细'}],
    summary:[{label:'订单数',value:'2',unit:'单'}],
    notes:['原始请求数量不换算，不展示跨单位数量合计，也不作为实际出库量。',
      '没有有效明细时数量和单位留空，不记为零。单位含义未确认。',
      '数仓同步完整性未验证，重复读取一致不代表原子快照。'],
    pagination:{result_id:`rp_${'S'.repeat(32)}`,page:number,page_count:2,total_rows:2,
      start_row:number,end_row:number,expires_at:expires,reconciled:true}});
  await page.route('**/v1/**',async route=>{
    const endpoint = route.request().url().split('?')[0];
    const match = endpoint.match(/\/v1\/results\/rp_S{32}\/pages\/(\d+)$/);
    let body = {};
    if(endpoint.endsWith('/v1/session')) body={status:'ready',identity_mode:'installation',conversation_id:conversation,
      csrf_token:'fixture_csrf_token_1234567890',clarification_pending:false,context_state:{scope:'principal',has_context:true,status:'ready'}};
    else if(endpoint.endsWith('/v1/conversations')) body={conversation_id:conversation};
    else if(match) {
      if(await page.evaluate(()=>window.so07PageExpired === true)) {
        await route.fulfill({status:410,contentType:'application/json',body:JSON.stringify({error:{code:'result_page_unavailable',message:'结果已过期，请重新查询'}})});
        return;
      }
      body={business_table:table(Number(match[1]))};
    } else if(endpoint.endsWith('/v1/tasks')) {
      const raw = table(1);
      if(await page.evaluate(()=>window.so07Invalid === true)) raw.summary.push({label:'合计',value:'99',unit:'箱'});
      body={task_id:'task_00000000000000000000000000000071',state:'completed',stage:'completed',http_status:200,
        result:{request_id:'so07_fixture',conversation_id:conversation,status:'completed',result_state:'live_success',
          outcome:{state:'completed'},clarification:{required:false,questions:[]},
          process:{state:'completed',steps:[{id:'query',state:'completed'}]},
          business_result:{format:'plain_text',text:'社团订单查询完成，数量与单位代码见表格。'},business_table:raw}};
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto(origin);
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
