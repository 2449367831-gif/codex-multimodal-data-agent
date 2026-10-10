async page => {
  let calls = 0;
  const conversation = 'c_turnover_window_fixture';
  const artifactId = 'art_' + 'w'.repeat(32);
  await page.unroute('**/v1/**');
  await page.route('**/v1/**', async route => {
    const path = route.request().url().split('?')[0];
    if (path.endsWith(`/v1/artifacts/${artifactId}`)) {
      await route.fulfill({contentType:'image/png', path:'output/playwright/turnover-window-fixture.png'});
      return;
    }
    let body = {};
    if (path.endsWith('/v1/session')) body = {status:'ready', identity_mode:'installation', conversation_id:conversation, csrf_token:'fixture_csrf_token_1234567890', clarification_pending:false, context_state:{scope:'principal',has_context:true,status:'ready'}};
    if (path.endsWith('/v1/clarification/resume')) body = {conversation_id:conversation, clarification:{required:false,questions:[]}};
    if (path.endsWith('/v1/conversations')) body = {conversation_id:conversation};
    if (path.endsWith('/v1/tasks')) {
      const submitted = route.request().postDataJSON();
      if (submitted.question !== '生成中心仓和分仓14日周转数据图片') throw new Error('retry changed the question');
      calls += 1;
      body = calls === 1 ? {
        task_id:'task_'+'1'.repeat(32), state:'failed', http_status:504,
        result:{error:{code:'query_timeout'}}
      } : {
        task_id:'task_'+'2'.repeat(32), state:'completed', http_status:200,
        result:{status:'completed', result_state:'live_result', conversation_id:conversation,
          outcome:{state:'completed'}, clarification:{required:false,questions:[]},
          process:{steps:[{id:'query',state:'completed',label:'Doris 查询已完成'}]},
          artifacts:[{artifact_id:artifactId,kind:'center_warehouse_turnover_7d',content_type:'image/png',
            title:'中心仓与分仓14日周转（合成验收数据）',period:'2026-08-15 至 2026-08-29（14个完整自然日）',url:`/v1/artifacts/${artifactId}`}]
        }
      };
    }
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:8021/');
  await page.evaluate(() => localStorage.clear());
  await page.reload();
}
