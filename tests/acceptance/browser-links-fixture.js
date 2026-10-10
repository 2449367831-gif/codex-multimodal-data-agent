async page => {
  await page.unroute('**/v1/**');
  await page.route('**/v1/**', async route => {
    const path = route.request().url().split('?')[0];
    let body = {};
    if (path.endsWith('/session')) body = {status:'ready',identity_mode:'installation',conversation_id:'c_links_fixture',csrf_token:'fixture_csrf_token_1234567890',context_state:{scope:'principal',has_context:true,status:'ready'}};
    if (path.endsWith('/conversations')) body = {conversation_id:'c_links_fixture'};
    if (path.endsWith('/tasks')) {
      const input = route.request().postDataJSON();
      const selected = Boolean(input.clarification_answers);
      const result = selected ? {
        status:'completed',result_state:'live_success',conversation_id:'c_links_fixture',outcome:{state:'completed'},clarification:{required:false,questions:[]},
        business_table:{format:'table',title:'出库单关联链路',caption:'全部时间 · 全部货主',empty_label:'查询成功，当前范围没有匹配的出库单',
          columns:[['cargo_owner','货主'],['outbound_code','出库单号'],['referenced_order_code','引用的销售订单'],['linked_sales_order_code','已匹配销售订单'],
            ['source_order_code','头部来源单号'],['detail_source_order_code','订单来源编码'],['source_platform_code','来源平台'],
            ['referenced_order_type','出库关联类型'],['order_type','订单类型'],['link_ship_at','出库时间'],['relationship_state','关联状态']].map(([key,label])=>({key,label,kind:'text',align:'left'})),
          rows:[{cargo_owner:'TEST-A',outbound_code:'TEST-OUT-1',referenced_order_code:'TEST-SO-1',linked_sales_order_code:'TEST-SO-1',source_order_code:'TEST-HEADER-1',detail_source_order_code:'TEST-LINE-001',source_platform_code:'TEST-PLATFORM',referenced_order_type:'JYCK',order_type:'SO01',link_ship_at:'2026-09-10 12:00:00',relationship_state:'已匹配销售订单'},
            {cargo_owner:'TEST-A',outbound_code:'TEST-OUT-1',referenced_order_code:'TEST-SO-1',linked_sales_order_code:'TEST-SO-1',source_order_code:'TEST-HEADER-1',detail_source_order_code:'TEST-LINE-002',source_platform_code:'TEST-PLATFORM',referenced_order_type:'JYCK',order_type:'SO01',link_ship_at:'2026-09-10 12:00:00',relationship_state:'已匹配销售订单'},
            {cargo_owner:'TEST-B',outbound_code:'TEST-OUT-2',referenced_order_code:'TEST-MISSING',linked_sales_order_code:null,source_order_code:null,detail_source_order_code:null,source_platform_code:null,referenced_order_type:null,order_type:null,link_ship_at:null,relationship_state:'未匹配销售订单'}],
          summary:[{label:'出库单数',value:'2',unit:'单'}],notes:['头部来源单号和订单来源编码分别来自销售订单主记录和有效明细。','未匹配时保留出库单，多个明细来源分别展示。','两次读取一致不等于原子快照，出库时间不能证明同步完整。']}
      } : {status:'needs_selection',result_state:'clarification_required',conversation_id:'c_links_fixture',outcome:{state:'clarification_required'},clarification:{required:true,token:'ct_fixture_link_selection_1234567890',questions:[{
        id:'outbound_link_scope',prompt:'未指定出库单号，本次查询哪一范围的关联链路？',selection:'single',options:[
          {id:'outbound_link_latest',label:'查询范围内最新出库的一张单'},{id:'outbound_link_all',label:'查询范围内全部出库单'}]}]}};
      body = {task_id:selected?'task_00000000000000000000000000000102':'task_00000000000000000000000000000101',state:'completed',http_status:200,result};
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
