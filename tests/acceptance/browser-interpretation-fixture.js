async (page) => {
  await page.unroute('**/v1/**');
  const conversation='c_meaning_fixture', entries=new Map(), proposals=new Map(), closed=new Set();
  const counts={creates:0,selects:0,resumes:0,closes:0,cancels:0,queryCalls:0,reviewResumes:0};
  const reply=(route,status,body)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  const project=entry=>JSON.parse(JSON.stringify(entry.result));
  const question=(i)=>({id:`q${i}`,prompt:i?'请选择适用范围':'请选择计量单位',selection:'single',
    options:i?[{id:'o0',label:'全部成品'},{id:'o2',label:'仅在售成品'},{id:'defer',label:'暂不确认'}]:[{id:'o0',label:'基本数量'},{id:'o2',label:'箱数'},{id:'defer',label:'暂不确认'}]});
  function ready(entry) {
    const id='kp_'+entry.result.interpretation_id.slice(3), decision='a'.repeat(64);
    entry.result.state='review_ready';entry.result.revision++;entry.result.questions=[];entry.result.choices={};entry.result.proposal={proposal_id:id};
    proposals.set(id,{binding:entry.binding,result:{proposal_id:id,revision:0,expires_at:2000000000,result_state:'definition_review',process:{state:'not_queried',steps:[]},assessment:{
      state:'proposal_assessed_not_executed',title:'口径变更评估',publication_authorized:false,requires_confirmation:true,assessment_complete:true,
      dependency_state:'已检查声明的依赖关系',notice:'仅评估本次提案，未查询数据、未发布口径；历史结果不能直接与变更后的口径比较。',choices:{},
      comparisons:[{decision_id:decision,metric:'成品库存',field:'指标名称',before:'可用成品库存',after:'测试库存名称'}],
      questions:[{id:decision,prompt:'成品库存：指标名称',selection:'single',options:[{id:'current',label:'保留当前定义'},{id:'proposed',label:'采用本次提案'},{id:'defer',label:'暂不确认'}]}]}}});
  }
  await page.route('**/v1/**', async route=>{
    const url=route.request().url().replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const input=route.request().method()==='POST'?route.request().postDataJSON():{};
    if(url==='/v1/session') return reply(route,200,{conversation_id:conversation,csrf_token:'fixture_csrf_token_1234567890',identity_mode:'installation',clarification_pending:false});
    if(url==='/v1/learning/summary') return reply(route,200,{});
    if(url==='/v1/fixture-counts') return reply(route,200,counts);
    if(url==='/v1/knowledge-proposal-sessions/close') {
      counts.closes++;closed.add(input.display_session_id);return reply(route,200,{state:'closed'});
    }
    if(url==='/v1/knowledge-interpretations') {
      counts.creates++;
      if(closed.has(input.display_session_id)) return reply(route,410,{error:{code:'interpretation_expired'}});
      const id='ki_'+String(counts.creates).padStart(32,'a');
      const entry={binding:input,mode:input.question,polls:0,result:{interpretation_id:id,revision:0,expires_at:2000000000,state:'running',questions:[],choices:{},proposal:null,error:null,
        result_state:'definition_interpretation',process:{state:'not_queried',steps:[]}}};
      entries.set(id,entry);
      if(input.question.includes('直接差异')) ready(entry);
      if(input.question.includes('丢失提交')) return route.abort('failed');
      if(input.question.includes('延迟')) await page.waitForTimeout(900);
      return reply(route,200,project(entry));
    }
    const match=url.match(/\/v1\/knowledge-interpretations\/(ki_[A-Za-z0-9_-]+)\/(resume|select|cancel)$/);
    if(match) {
      const e=entries.get(match[1]),action=match[2];
      if(!e || e.binding.display_session_id!==input.display_session_id || e.binding.request_id!==input.request_id || input.conversation_id!==conversation) return reply(route,404,{error:{code:'interpretation_unavailable'}});
      if(closed.has(input.display_session_id) || e.mode.includes('过期')) return reply(route,410,{error:{code:'interpretation_expired'}});
      if(action==='cancel') {counts.cancels++;e.result.state='cancelled';e.result.questions=[];e.result.choices={};e.result.error=null;e.result.proposal=null;}
      if(action==='resume') {
        counts.resumes++;
        if(e.mode.includes('慢恢复')) {
          await page.evaluate(()=>{window.meaningReadPending=true;});
          await page.waitForTimeout(1000);
        }
        if(e.result.state==='running' && !e.mode.includes('持续')) {
          if(e.result.revision>1) ready(e);
          else {e.result.revision++;e.result.state='needs_confirmation';e.result.questions=[question(0),question(1)];}
        }
        if(e.mode.includes('不安全')) e.result.questions[0].prompt='SELECT PRIVATE FROM hidden_table';
      }
      if(action==='select') {
        counts.selects++;
        if(e.result.revision!==input.revision) return reply(route,409,{error:{code:'interpretation_conflict'}});
        if(Object.keys(input.choices).length!==2) return reply(route,422,{error:{code:'interpretation_invalid'}});
        e.result.revision++;e.result.choices=input.choices;
        e.result.state=Object.values(input.choices).includes('defer')?'deferred':'running';
        if(e.mode.includes('丢失选择')) {e.mode='normal';return route.abort('failed');}
      }
      return reply(route,200,project(e));
    }
    const review=url.match(/\/v1\/knowledge-proposals\/(kp_[A-Za-z0-9_-]+)\/(resume|select|cancel)$/);
    if(review) {
      const e=proposals.get(review[1]);
      if(!e || e.binding.display_session_id!==input.display_session_id || e.binding.request_id!==input.request_id) return reply(route,404,{error:{code:'proposal_unavailable'}});
      if(review[2]==='resume') counts.reviewResumes++;
      if(review[2]==='cancel') {proposals.delete(review[1]);return reply(route,200,{state:'cancelled'});}
      if(review[2]==='select') {e.result.revision++;e.result.assessment.choices=input.choices;e.result.assessment.requires_confirmation=false;e.result.assessment.state='proposal_selected_not_executed';}
      return reply(route,200,e.result);
    }
    counts.queryCalls++;return reply(route,500,{error:{code:'unexpected_query'}});
  });
  await page.goto('http://127.0.0.1:8021/static/assets/royal-tiger/tiger-avatar.jpg');
  await page.evaluate(()=>localStorage.clear());
  await page.goto('http://127.0.0.1:8021/');
}
