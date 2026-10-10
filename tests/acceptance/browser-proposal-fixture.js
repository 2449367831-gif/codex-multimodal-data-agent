async (page) => {
  await page.unroute('**/v1/**');
  const conversation='c_proposal_fixture', entries=new Map(), closed=new Set();
  let count=0, selections=0, closes=0, resumes=0, queryCalls=0;
  const options=[{id:'current',label:'保留当前定义'},{id:'proposed',label:'采用本次提案'},{id:'defer',label:'暂不确认'}];
  const reply=(route,status,body)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  await page.route('**/v1/**', async route => {
    const url=route.request().url().replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const input=route.request().method()==='POST' ? route.request().postDataJSON() : {};
    if(url==='/v1/session') return reply(route,200,{conversation_id:conversation,csrf_token:'fixture_csrf_token_1234567890',identity_mode:'installation',clarification_pending:false});
    if(url==='/v1/learning/summary') return reply(route,200,{});
    if(url==='/v1/knowledge-proposal-sessions/close') {closed.add(input.display_session_id);closes++;return reply(route,200,{state:'closed'});}
    if(url==='/v1/knowledge-proposals') {
      const material=JSON.parse(input.proposal), mode=material.changes[0].set.notes;
      if(closed.has(input.display_session_id)) return reply(route,410,{error:{code:'proposal_expired'}});
      const proposal='kp_'+String(++count).padStart(32,'a');
      const quantity=mode==='many' ? 14 : 2;
      const comparisons=Array.from({length:quantity},(_,i)=>({decision_id:i.toString(16).padStart(64,'0'),metric:`演示指标${i+1}`,field:'口径说明',before:'汇总后按当前业务定义计算',after:'候选定义，仍需对账与审核'}));
      const result={result_state:'definition_review',process:{state:'not_queried',steps:[]},proposal_id:proposal,revision:0,expires_at:2000000000,
        assessment:{state:'proposal_assessed_not_executed',title:'口径变更评估',comparisons,questions:comparisons.map(row=>({id:row.decision_id,prompt:row.metric+'：'+row.field,selection:'single',options})),choices:{},requires_confirmation:true,assessment_complete:true,publication_authorized:false,
          notice:'仅评估本次提案，未查询数据、未发布口径；历史结果不能直接与变更后的口径比较。',dependency_state:'已检查声明的依赖关系'}};
      if(mode==='unsafe') result.assessment.comparisons[0].after='SELECT PRIVATE_RESPONSE FROM hidden_table';
      entries.set(proposal,{result,binding:input,mode});
      if(mode==='late') await page.waitForTimeout(700);
      await page.evaluate(flags=>window.proposalFixtureFlags=flags,{count,selections,closes,resumes,queryCalls,exactDecimal:input.proposal.includes('0.1234567890123456789')});
      return reply(route,200,result);
    }
    const match=url.match(/\/v1\/knowledge-proposals\/(kp_[A-Za-z0-9_-]+)\/(resume|select|cancel)$/);
    if(match) {
      const entry=entries.get(match[1]),action=match[2];
      if(!entry || entry.binding.display_session_id!==input.display_session_id || entry.binding.request_id!==input.request_id || input.conversation_id!==conversation) return reply(route,404,{error:{code:'proposal_unavailable'}});
      if(action==='cancel') {entries.delete(match[1]);return reply(route,200,{state:'cancelled'});}
      if(closed.has(input.display_session_id) || entry.mode==='expired') return reply(route,410,{error:{code:'proposal_expired'}});
      if(action==='resume') resumes++;
      if(action==='select') {
        selections++;
        if(entry.result.revision!==input.revision) return reply(route,409,{error:{code:'proposal_conflict'}});
        entry.result.revision++;
        entry.result.assessment.choices=input.choices;
        const deferred=Object.values(input.choices).includes('defer');
        entry.result.assessment.requires_confirmation=deferred;
        entry.result.assessment.state=deferred?'proposal_deferred_not_executed':'proposal_selected_not_executed';
        if(entry.mode==='lost') {entry.mode='normal';return route.abort('failed');}
      }
      await page.evaluate(flags=>window.proposalFixtureFlags=flags,{count,selections,closes,resumes,queryCalls});
      return reply(route,200,entry.result);
    }
    queryCalls++;
    return reply(route,500,{error:{code:'unexpected_query'}});
  });
  await page.goto('http://127.0.0.1:8021/static/assets/royal-tiger/tiger-avatar.jpg');
  await page.evaluate(()=>localStorage.clear());
  await page.goto('http://127.0.0.1:8021/');
}
