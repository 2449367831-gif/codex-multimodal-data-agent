async page => {
  await page.unroute('**/v1/**');
  await page.route('**/v1/**',async route => {
    const path=route.request().url().split('?')[0];
    let body={};
    if(path.endsWith('/session')) body={status:'ready',identity_mode:'installation',conversation_id:'c_names_fixture',csrf_token:'fixture_csrf_token_1234567890',context_state:{scope:'principal',has_context:true,status:'ready'}};
    if(path.endsWith('/conversations')) body={conversation_id:'c_names_fixture'};
    if(path.endsWith('/tasks')) {
      const submitted=route.request().postDataJSON();
      const selected=Boolean(submitted.clarification_answers);
      const result=selected ? {
        status:'completed',result_state:'live_success',conversation_id:'c_names_fixture',outcome:{state:'completed'},clarification:{required:false,questions:[]},
        business_table:{format:'table',title:'SKU名称与实际出库核对',caption:'全部时间 · 全部货主 · 全部仓库',empty_label:'查询成功，当前范围没有记录',
          columns:[['cargo_owner','货主','text'],['sku_code','SKU','text'],['sku_name','商品','text'],['quantity','实际销售出库','number','EA'],['outbound_count','出库单数','number','单'],
            ['shipment_name','出库记录名称','text'],['order_name','订单记录名称','text'],['master_name_variants','主数据名称数','number'],['missing_name_rows','空名称记录数','number'],['missing_name_sources','缺失来源数','number'],['name_variants','不同名称数','number'],['name_status','名称状态','text']
          ].map(([key,label,kind,unit])=>({key,label,kind,...(unit?{unit}:{})})),
          rows:[{cargo_owner:'TEST',sku_code:'TEST-SKU-1',sku_name:'测试主数据商品名称',quantity:'3.75',outbound_count:'2',shipment_name:'测试出库名称',order_name:'测试订单名称',master_name_variants:'1',missing_name_rows:'0',missing_name_sources:'0',name_variants:'3',name_status:'主数据名称；名称不一致'},
            {cargo_owner:'TEST',sku_code:'TEST-SKU-2',sku_name:'TEST-SKU-2',quantity:'0.00',outbound_count:'1',shipment_name:null,order_name:null,master_name_variants:'2',missing_name_rows:'1',missing_name_sources:'1',name_variants:'2',name_status:'显示编码；名称不一致；存在缺失'}],
          summary:[{label:'实际销售出库',value:'3.75',unit:'EA'},{label:'出库单数',value:'3',unit:'单'}],
          notes:['展示采用当前主数据唯一名称，缺失或冲突时显示编码。','空名称记录数不等于缺失来源数；名称按来源独立核对。','两次读取一致不等于原子快照。']}
      } : {status:'needs_selection',result_state:'clarification_required',conversation_id:'c_names_fixture',outcome:{state:'clarification_required'},clarification:{required:true,token:'ct_fixture_name_selection_1234567890',questions:[{
        id:'sku_name_outbound_basis',prompt:'名称检查对应哪种出库口径？',selection:'single',options:[{id:'sku_name_actual',label:'实际销售出库，核对名称'},{id:'sku_name_defer',label:'其他出库口径，暂不执行'}]}]}};
      body={task_id:selected?'task_00000000000000000000000000000092':'task_00000000000000000000000000000091',state:'completed',http_status:200,result};
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
