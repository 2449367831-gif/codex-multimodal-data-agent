async (page) => {
  await page.unroute("**/v1/**");
  const conversation = "c_ranked_sales_fixture";
  const resultId = `rr_${"R".repeat(32)}`;
  const expiresAt = Math.floor(Date.now() / 1000) + 3600;
  const columns = (keys) => keys.map(([key, label]) => ({key, label}));
  const ranking = () => ({format:"table",title:"成品销售出库排名",caption:"2026-09-10 · 全部货主与仓库",empty_label:"当前范围无数据",
    columns:columns([["rank","排名"],["sku_code","SKU"],["sku_name","商品"],["quantity","实发数量"],["outbound_count","出库单数"]]),
    rows:[{rank:"1",sku_code:"TEST-A",sku_name:"测试成品甲",quantity:"9007199254740993.25",outbound_count:"3"},
      {rank:"2",sku_code:"TEST-B",sku_name:"测试成品乙长名称用于验证移动端可读性",quantity:"2.5",outbound_count:"1"}],
    summary:[{label:"入榜实发数量",value:"9007199254740995.75",unit:"EA"},{label:"入榜出库单数",value:"4",unit:"单"}],notes:[],
    drilldown:{result_id:resultId,expires_at:expiresAt}});
  const detail = (sku, number=1) => ({format:"table",title:"成品销售出库明细",caption:"2026-09-10 · 原排名范围",empty_label:"当前范围无数据",
    columns:columns([["sku_code","SKU"],["cargo_owner","货主"],["warehouse_code","仓库编码"],["warehouse_name","仓库名称"],["customer_code","客户编码"],["outbound_code","出库单号"],["ship_at","出库时间"],["quantity","实发数量"]]),
    rows:(sku==="TEST-A" ? (number===1 ? [1,2] : [3]) : [4]).map((n)=>({sku_code:sku,cargo_owner:"TEST-OWNER",warehouse_code:`W${n}`,warehouse_name:"测试仓库",customer_code:n===2?null:"000012345678901234567890",outbound_code:`TEST-OUT-${n}`,ship_at:"2026-09-10 12:00:00+08:00",quantity:n===1?"9007199254740991.25":n===4?"2.5":"1"})),
    summary:[{label:"实发数量",value:sku==="TEST-A"?"9007199254740993.25":"2.5",unit:"EA"},{label:"出库单数",value:sku==="TEST-A"?"3":"1",unit:"单"}],notes:["完整明细与原排名已对账。"],
    ...(sku==="TEST-A"?{pagination:{result_id:`rp_${"P".repeat(32)}`,page:number,page_count:2,total_rows:3,start_row:number===1?1:3,end_row:number===1?2:3,expires_at:expiresAt,reconciled:true}}:{})});
  const tasks = new Map();
  let mode="success", pageFailure=false, dropNextPost=false, sequence=0, detailPosts=0, detailGets=0;
  const result = (table, selection) => ({request_id:"ranked_fixture",conversation_id:conversation,status:"completed",result_state:"live_success",outcome:{state:"completed"},
    clarification:{required:false,questions:[]},process:{steps:[{id:"query",state:"completed"}]},business_table:table,
    ...(selection?{drilldown_source:selection}:{})});
  const receipt = (task) => {
    if(task.selection && mode==="pending") return {task_id:task.id,state:"running",stage:"query"};
    if(task.selection && ["expired","failed"].includes(mode)) return {task_id:task.id,state:"failed",http_status:mode==="expired"?410:503,result:{error:{code:mode==="expired"?"ranking_unavailable":"semantic_query_error"}}};
    const table=task.selection?detail(task.selection.sku_code):ranking();
    if(!task.selection && mode==="invalid-ranking") table.drilldown.result_id="invalid";
    table.internal_plan="PRIVATE-CONTEXT-MARKER";
    if(task.selection && mode==="wrong-sku") table.rows[0].sku_code="FOREIGN-SKU";
    if(task.selection && mode==="wrong-total") table.summary[0].value="9007199254740993.26";
    const selection=task.selection&&{...task.selection};
    if(selection && mode==="wrong-source") selection.result_id=`rr_${"X".repeat(32)}`;
    return {task_id:task.id,state:task.selection&&mode==="bad-receipt"?"unknown":"completed",stage:"completed",http_status:200,result:result(table,selection)};
  };
  await page.route("**/v1/**", async (route) => {
    const req=route.request(), path=req.url().replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    let body={};
    if(path==="/v1/fixture") {
      const settings=req.method()==="POST"?req.postDataJSON():{};
      mode=settings.mode??mode; pageFailure=settings.pageFailure??pageFailure; dropNextPost=settings.dropNextPost??dropNextPost;
      body={detailPosts,detailGets,tasks:tasks.size};
    } else if(path==="/v1/session") body={status:"ready",identity_mode:"installation",conversation_id:conversation,csrf_token:"fixture_csrf_token_1234567890",clarification_pending:false,context_state:{scope:"principal",has_context:true,status:"ready"}};
    else if(path==="/v1/conversations") body={conversation_id:conversation};
    else if(path==="/v1/tasks") {
      const request=req.postDataJSON(), key=req.headers()["idempotency-key"];
      if(request.conversation_id!==conversation) throw new Error("Wrong conversation");
      let task=tasks.get(key);
      if(!task) {
        const selection=request.drilldown;
        if(selection) {
          if(Object.keys(request).sort().join()!=="conversation_id,drilldown" || Object.keys(selection).sort().join()!=="result_id,sku_code" || selection.result_id!==resultId) throw new Error("Selection changed scope");
          detailPosts++;
        }
        task={id:`task_${(++sequence).toString(16).padStart(32,"0")}`,selection}; tasks.set(key,task);
      }
      if(task.selection && dropNextPost) {dropNextPost=false;await route.abort("failed");return;}
      body=receipt(task);
    } else if(path.startsWith("/v1/tasks/")) {
      const task=[...tasks.values()].find(t=>path.endsWith(t.id));
      if(!task) throw new Error("Unknown receipt");
      if(task.selection) detailGets++;
      body=receipt(task);
    } else if(path.startsWith("/v1/results/")) {
      if(pageFailure) {await route.fulfill({status:410,json:{error:{code:"result_page_unavailable"}}});return;}
      body={business_table:detail("TEST-A",Number(path.split("/").pop()))};
    }
    await route.fulfill({status:200,json:body});
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
