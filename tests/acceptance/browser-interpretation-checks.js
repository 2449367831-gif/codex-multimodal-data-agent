async (page) => {
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const counts=()=>page.evaluate(()=>fetch('/v1/fixture-counts').then(r=>r.json()));
  const submit=async text=>{
    await page.waitForFunction(()=>!document.querySelector('#proposalUpload').disabled);
    await page.getByRole('radio',{name:'调整口径',exact:true}).check();
    await page.locator('#questionInput').fill(text);await page.locator('#questionInput').press('Enter');
  };
  const choose=async(id,value)=>page.locator(`.interpretation-review [data-question="${id}"][value="${value}"]`).check();
  const privacy=async()=>{
    const checks=await page.evaluate(()=>({
      noPrivate:!(/PRIVATE|SELECT|baseline_sha256|hidden_table/.test(document.body.innerText)),
      noMaterial:!(/请选择计量单位|测试库存名称|基本数量|baseline_sha256/.test(JSON.stringify(localStorage))),
      noFakeQuery:!document.querySelector('.interpretation-review .execution-card')&&!document.querySelector('#traceProcess').children.length,
      noOverflow:document.documentElement.scrollWidth<=innerWidth,
      assets:[...document.images].filter(i=>i.getBoundingClientRect().width).every(i=>i.complete&&i.naturalWidth)
    }));
    if(Object.values(checks).some(v=>!v)) throw Error(JSON.stringify(checks));
  };
  await page.setViewportSize({width:1440,height:1000});
  await submit('将成品库存的名称调整为测试库存');
  await page.getByRole('button',{name:'确认全部选择'}).waitFor();
  await page.getByRole('button',{name:'确认全部选择'}).click();
  await page.getByText('请完成本次全部选择后一次提交。').waitFor();
  if((await counts()).selects!==0) throw Error('Partial choices submitted');
  await choose('q0','o2');await page.reload();
  await page.getByRole('button',{name:'确认全部选择'}).waitFor();
  if(!await page.locator('[data-question="q0"][value="o2"]').isChecked()) throw Error('Draft choice lost');
  await choose('q1','defer');await page.getByRole('button',{name:'确认全部选择'}).click();
  await page.getByText('已保留选择，暂未继续').waitFor();
  await page.reload();await page.getByText('已保留选择，暂未继续').waitFor();
  await privacy();await page.screenshot({path:'output/playwright/interpretation-desktop.png',animations:'disabled'});
  await page.emulateMedia({reducedMotion:'reduce'});
  for(const size of [{width:375,height:812},{width:812,height:375}]) {
    await page.setViewportSize(size);await privacy();
    const reachable=await page.evaluate(()=>{
      const s=document.querySelector('#chatScroll');s.scrollTop=s.scrollHeight;
      const b=document.querySelector('.interpretation-review .proposal-actions').getBoundingClientRect(), c=document.querySelector('.composer-area').getBoundingClientRect();
      return b.bottom<=c.top && b.top>=s.getBoundingClientRect().top;
    });
    if(!reachable) throw Error('Choice actions obscured');
    await page.screenshot({path:`output/playwright/interpretation-${size.width}.png`});
  }
  await page.setViewportSize({width:1440,height:1000});await page.getByRole('button',{name:'切换外观'}).click();
  await privacy();await page.screenshot({path:'output/playwright/interpretation-dark.png'});
  await choose('q1','o0');await page.getByRole('button',{name:'确认全部选择'}).click();
  await page.getByRole('button',{name:'一次提交全部选择'}).waitFor();
  await page.locator('.proposal-review:not(.interpretation-review) input[value="current"]').check();
  await page.getByRole('button',{name:'一次提交全部选择'}).click();await page.getByText('选择已记录，尚未发布').waitFor();
  await page.reload();await page.getByText('选择已记录，尚未发布').waitFor();
  if(await page.getByRole('button',{name:'一次提交全部选择'}).count()) throw Error('Ready resume revived old proposal');
  if(await page.locator('.proposal-review:not(.interpretation-review)').count()!==1) throw Error('Duplicate handoff');
  if(await page.getByText('将成品库存的名称调整为测试库存',{exact:true}).count()!==1) throw Error('Original question missing');
  await page.getByRole('button',{name:'新建界面会话'}).click();
  if(await page.locator('.interpretation-review').count()) throw Error('Cross-display leak');
  await page.locator('[data-session-id]').nth(1).click();await page.getByText('选择已记录，尚未发布').waitFor();
  await page.getByRole('button',{name:'清空当前显示'}).click();await page.waitForFunction(()=>!document.querySelector('.proposal-review'));
  await submit('持续整理的测试口径');await page.getByText('正在整理口径',{exact:true}).waitFor();
  await page.reload();await page.getByText('正在整理口径',{exact:true}).waitFor();
  const beforeCancel=await counts();
  if(beforeCancel.creates!==2) throw Error('Refresh restarted model');
  await page.getByRole('button',{name:'取消口径整理'}).click();await page.getByText('口径整理已取消').waitFor();
  await page.reload();await page.getByText('口径整理已取消').waitFor();
  await page.locator('.conversation-item-row:has([aria-current="page"]) [data-delete-session-id]').click();
  await page.waitForFunction(()=>!document.querySelector('.interpretation-review'));
  await page.reload();await page.waitForFunction(()=>!document.querySelector('#proposalUpload').disabled);
  if(await page.locator('.interpretation-review').count()) throw Error('Deleted conversation restored');
  const final=await counts();if(final.queryCalls || final.creates!==2 || final.selects!==2 || final.cancels!==1 || final.closes!==2 || !final.reviewResumes) throw Error(JSON.stringify(final));
  if(errors.length) throw Error(errors.join('\n'));
  return {allChoices:true,originalQuestion:true,draftRefresh:true,defer:true,referenceHandoff:true,finalRefresh:true,runningRefresh:true,cancel:true,delete:true,clear:true,isolation:true,privacy:true,mobile:true,landscape:true,dark:true,noQuery:true};
}
