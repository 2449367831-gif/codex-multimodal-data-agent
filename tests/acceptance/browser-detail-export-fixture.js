async (page) => {
  await page.unroute('**/v1/**');
  const id='ex_'+'e'.repeat(32), conversation='c_export_fixture';
  let mode='interrupt', reads=[];
  const meta={export_id:id,total_rows:5,chunk_rows:2,chunks:3,expires_at:Math.floor(Date.now()/1000)+86400};
  await page.addInitScript(() => {
    window.showSaveFilePicker=async()=> (await navigator.storage.getDirectory()).getFileHandle('synthetic-export.csv',{create:true});
  });
  await page.route('**/v1/**', async route => {
    const request=route.request(), path=request.url().replace(/^https?:\/\/[^/]+/,'').split('?')[0];
    let body={};
    if(path==='/v1/fixture') {
      if(request.method()==='POST')mode=request.postDataJSON().mode;
      body={mode,reads};
    } else if(path==='/v1/session')body={status:'ready',identity_mode:'installation',conversation_id:conversation,
      csrf_token:'export_fixture_csrf_1234567890',clarification_pending:false,context_state:{scope:'principal',has_context:true,status:'ready'}};
    else if(path==='/v1/conversations')body={conversation_id:conversation};
    else if(path==='/v1/tasks')body={task_id:'task_'+'1'.repeat(32),state:'completed',http_status:200,result:{
      request_id:'task_'+'1'.repeat(32),conversation_id:conversation,status:'completed',result_state:'summary_only',
      clarification:{required:false,questions:[]},context_state:{scope:'principal',has_context:true,status:'ready'},
      process:{steps:[{id:'query',state:'completed'}]},business_table:{format:'table',title:'实际销售出库范围汇总',
        caption:'2026-09-01 至 2026-09-15 · 全部货主 · 全部仓库',empty_label:'没有数据',
        columns:[{key:'quantity',label:'实发数量'},{key:'outbound_count',label:'出库单数'}],
        rows:[{quantity:'15',outbound_count:'5'}],summary:[],notes:[],detail_batches:{result_id:'db_'+'E'.repeat(32),
          next_batch:1,loaded_rows:0,total_rows:5,complete:false,expires_at:meta.expires_at,export_id:id}}}};
    else if(path===`/v1/exports/${id}`)body=meta;
    else if(path.startsWith(`/v1/exports/${id}/chunks/`)) {
      const index=Number(path.split('/').pop());reads.push(index);
      if(request.method()!=='POST'||request.postData()||!request.headers()['x-csrf-token'])throw Error('invalid export request');
      if(mode==='interrupt'&&index===1)return route.abort('failed');
      if(mode==='changed')return route.fulfill({status:409,json:{error:{code:'detail_source_changed',message:'INTERNAL MUST NOT DISPLAY'}}});
      const text=(index===0?'\ufeff货主,仓库编码,出库单号,SKU编码,出库时间,实发数量（EA）\r\n':'')+
        Array.from({length:index===2?1:2},(_,i)=>{const n=index*2+i+1;return `TEST,TEST-WH,TEST-OUT-${n},TEST-SKU-${n},2026-09-10 12:00:00,${n}\r\n`;}).join('');
      const hash=await page.evaluate(async text=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text))),b=>b.toString(16).padStart(2,'0')).join(''),text);
      return route.fulfill({status:200,body:text,headers:{'Content-Type':'text/csv; charset=utf-8',
        'X-Export-Id':id,'X-Chunk-Index':String(index),'X-Chunk-Rows':String(index===2?1:2),
        'X-Loaded-Rows':String(Math.min(index*2+2,5)),'X-Export-Complete':String(index===2),
        'X-Chunk-SHA256':mode==='bad-hash'?'0'.repeat(64):hash}});
    }
    await route.fulfill({status:200,json:body});
  });
  await page.goto('http://127.0.0.1:8021/health');
  await page.evaluate(async()=>{
    localStorage.clear();
    await new Promise((resolve,reject)=>{const r=indexedDB.deleteDatabase('royal-tiger-detail-exports');r.onsuccess=resolve;r.onerror=reject;});
    const directory=await navigator.storage.getDirectory();
    await directory.removeEntry('synthetic-export.csv').catch(()=>{});
  });
  await page.goto('http://127.0.0.1:8021/');
}
