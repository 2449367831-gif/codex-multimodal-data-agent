async (page) => {
  await page.unroute("**/v1/**");
  const conversation = "c_detail_batches_fixture";
  const id = `db_${"B".repeat(32)}`;
  const expires = Math.floor(Date.now() / 1000) + 3600;
  const metadata = loaded => ({result_id:id, next_batch:loaded===0?1:loaded===3?2:3,
    loaded_rows:loaded,total_rows:5,complete:loaded===5,expires_at:expires});
  const columns = keys => keys.map(([key,label]) => ({key,label}));
  const summary = () => ({format:"table",title:"实际销售出库范围汇总",caption:"2026-09-01 至 2026-09-15 · 全部货主 · 全部仓库",empty_label:"当前范围无数据",
    columns:columns([["quantity","实发数量"],["outbound_count","出库单数"]]),rows:[{quantity:"15",outbound_count:"4"}],
    summary:[],notes:["当前仅完成汇总，明细尚未全部取回。"],detail_batches:metadata(0)});
  const detail = (index, number=1) => ({format:"table",title:`实际销售出库明细 · 第${index}批`,caption:"2026-09-01 至 2026-09-15 · 全部货主 · 全部仓库",empty_label:"当前范围无数据",
    columns:columns([["cargo_owner","货主"],["warehouse_code","仓库编码"],["outbound_code","出库单号"],["sku_code","SKU"],["ship_at","出库时间"],["quantity","实发数量"]]),
    rows:(index===1?(number===1?[1,2]:[3]):[4,5]).map(n=>({cargo_owner:"TEST",warehouse_code:"TEST-WH",outbound_code:`TEST-OUT-${n}`,sku_code:n===1?"=FORMULA-TEST":`TEST-SKU-${n}`,ship_at:"2026-09-10 12:00:00+08:00",quantity:String(n)})),
    summary:[{label:"实发数量",value:index===1?"6":"9",unit:"EA"},{label:"出库单数",value:index===1?"3":"2",unit:"单"}],
    notes:[index===1?"本批已对账，原范围明细尚未全部取回。":"全部批次已累计对账。"],detail_batches:metadata(index===1?3:5),
    ...(index===1?{pagination:{result_id:`rp_${"P".repeat(32)}`,page:number,page_count:2,total_rows:3,
      start_row:number===1?1:3,end_row:number===1?2:3,expires_at:expires,reconciled:true}}:{})});
  let mode="success",sequence=0,posts=0,gets=0;
  const tasks = new Map();
  const receipt = task => {
    if(task.selection && mode==="pending") return {task_id:task.id,state:"running",stage:"query"};
    if(task.selection && mode==="changed") return {task_id:task.id,state:"failed",http_status:409,result:{error:{code:"detail_source_changed"}}};
    const selection = task.selection && {...task.selection};
    if(selection && mode==="wrong-source") selection.result_id=`db_${"X".repeat(32)}`;
    return {task_id:task.id,state:"completed",http_status:200,result:{request_id:task.id,conversation_id:conversation,status:"completed",
      result_state:!selection?"summary_only":selection.batch_index===2?"live_success":"batch_complete",outcome:{state:"completed"},
      clarification:{required:false,questions:[]},context_state:{scope:"principal",has_context:true,status:"ready"},
      process:{steps:[{id:"query",state:"completed"}]},business_table:selection?detail(selection.batch_index):summary(),
      ...(selection?{detail_batch_source:selection}:{})}};
  };
  await page.route("**/v1/**",async route=>{
    const req=route.request(),path=req.url().replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    let body={};
    if(path==="/v1/fixture") {const options=req.method()==="POST"?req.postDataJSON():{};mode=options.mode??mode;body={posts,gets,tasks:tasks.size};}
    else if(path==="/v1/session") body={status:"ready",identity_mode:"installation",conversation_id:conversation,csrf_token:"fixture_csrf_token_1234567890",clarification_pending:false,context_state:{scope:"principal",has_context:true,status:"ready"}};
    else if(path==="/v1/conversations") body={conversation_id:conversation};
    else if(path==="/v1/tasks") {
      const request=req.postDataJSON(),key=req.headers()["idempotency-key"];
      if(request.conversation_id!==conversation) throw new Error("wrong conversation");
      let task=tasks.get(key);
      if(!task){
        if(request.detail_batch){
          if(Object.keys(request).sort().join()!=="conversation_id,detail_batch" || Object.keys(request.detail_batch).sort().join()!=="batch_index,result_id" || request.detail_batch.result_id!==id) throw new Error("scope override in batch request");
          posts++;
        }
        task={id:`task_${(++sequence).toString(16).padStart(32,"0")}`,selection:request.detail_batch};tasks.set(key,task);
      }
      body=receipt(task);
    } else if(path.startsWith("/v1/tasks/")) {const task=[...tasks.values()].find(t=>path.endsWith(t.id));if(!task)throw new Error("unknown task");gets++;body=receipt(task);}
    else if(path.startsWith("/v1/results/")) body={business_table:detail(1,Number(path.split("/").pop()))};
    await route.fulfill({status:200,json:body});
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
