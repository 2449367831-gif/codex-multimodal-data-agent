async page => {
  await page.unroute("**/v1/**");
  await page.route("**/v1/**",async route => {
    const path=route.request().url().split("?")[0];
    let body={};
    if(path.endsWith("/session")) body={status:"ready",identity_mode:"installation",conversation_id:"c_boxes_fixture",csrf_token:"fixture_csrf_token_1234567890",context_state:{scope:"principal",has_context:true,status:"ready"}};
    if(path.endsWith("/conversations")) body={conversation_id:"c_boxes_fixture"};
    if(path.endsWith("/tasks")) body={task_id:"task_00000000000000000000000000000008",state:"completed",http_status:200,result:{
      status:"completed",result_state:"live_success",conversation_id:"c_boxes_fixture",outcome:{state:"completed"},clarification:{required:false,questions:[]},
      business_table:{format:"table",title:"成品实际出库与箱数",caption:"测试自然日 · 全部货主 · 全部仓库",empty_label:"当前范围没有有效出库",
        columns:[
          ["ship_date","出库日期","text"],["cargo_owner","货主","text"],["warehouse_code","仓库编码","text"],["warehouse_name","仓库名称","text"],["sku_code","SKU","text"],["customer_channel","客户渠道","text"],
          ["quantity","实际销售出库","number","EA"],["outbound_count","出库单数","number","单"],["box_spec","箱规","number","EA/箱"],["box_quantity","实发箱数","number","箱"],["box_state","换算状态","text"]
        ].map(([key,label,kind,unit])=>({key,label,kind,...(unit?{unit}:{})})),
        rows:["已换算","缺少箱规","箱规冲突"].map((status,i)=>({ship_date:"2026-09-09",cargo_owner:"TEST",warehouse_code:"TEST-W",warehouse_name:"测试中心仓",sku_code:`TEST-SKU-${i}`,customer_channel:["线上","线下","其他"][i],quantity:["15.00","4.25","8.50"][i],outbound_count:"1",box_spec:i===0?"10":null,box_quantity:i===0?"2":null,box_state:status})),
        summary:[{label:"实际销售出库",value:"27.75",unit:"EA"},{label:"出库单数",value:"3",unit:"单"}],
        notes:["按日期、货主、仓库、SKU和客户渠道汇总后换算，四舍五入不保留小数。","空值不是零箱；缺失与冲突逐行保留。","当前箱规，数据同步鲜度尚未确认。"]
      }
    }};
    await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(body)});
  });
  await page.goto("http://127.0.0.1:8021/");
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
}
