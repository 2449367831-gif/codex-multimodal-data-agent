async (page) => {
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const upload=async mode=>{
    await page.locator('#proposalFile').setInputFiles(`tests/fixtures/proposal/${mode}.json`);
    await page.waitForFunction(()=>!document.querySelector('.proposal-review:last-child[aria-busy="true"]'));
  };
  const choose=async (index,value)=>page.locator('.proposal-field').nth(index).locator(`input[value="${value}"]`).check();
  const privacy=async()=>{
    const flags=await page.evaluate(()=>({
      noPrivate:!(/PRIVATE|SELECT|baseline_sha256|test.metric/.test(document.body.innerText)),
      noStorage:!(/PRIVATE|baseline_sha256|候选定义|当前业务定义/.test(JSON.stringify(localStorage))),
      noFakeQuery:!document.querySelector('.proposal-message .execution-card') && !document.querySelector('#traceProcess').children.length,
      noOverflow:document.documentElement.scrollWidth<=innerWidth,
      assets:[...document.images].filter(img=>img.getBoundingClientRect().width).every(img=>img.complete&&img.naturalWidth)
    }));
    if(Object.values(flags).some(value=>!value)) throw new Error(JSON.stringify(flags));
  };
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>!document.querySelector('#proposalUpload').disabled);
  await upload('normal');
  await page.getByRole('button',{name:'一次提交全部选择'}).waitFor();
  if(!(await page.evaluate(()=>window.proposalFixtureFlags.exactDecimal))) throw new Error('Proposal decimal changed');
  await page.getByRole('button',{name:'一次提交全部选择'}).click();
  await page.getByText('请完成全部字段的选择后一次提交。').waitFor();
  await choose(0,'proposed');
  await page.reload();
  await page.getByRole('button',{name:'一次提交全部选择'}).waitFor();
  if(!await page.locator('.proposal-field').first().locator('input[value="proposed"]').isChecked()) throw new Error('Draft choice lost');
  await choose(1,'defer');
  await page.getByRole('button',{name:'一次提交全部选择'}).click();
  await page.waitForFunction(()=>!document.querySelector('.proposal-review[aria-busy="true"]'));
  await page.reload();
  await page.getByRole('button',{name:'一次提交全部选择'}).waitFor();
  if(!await page.locator('.proposal-field').nth(1).locator('input[value="defer"]').isChecked()) throw new Error('Deferred choice lost');
  await privacy();
  await page.screenshot({path:'output/playwright/proposal-desktop.png'});
  await page.emulateMedia({reducedMotion:'reduce'});
  for(const size of [{width:375,height:812},{width:812,height:375}]) {
    await page.setViewportSize(size);await privacy();
    const reachable=await page.evaluate(()=>{
      const scroll=document.querySelector('#chatScroll');scroll.scrollTop=scroll.scrollHeight;
      const button=document.querySelector('.proposal-actions button').getBoundingClientRect();
      const composer=document.querySelector('.composer-area').getBoundingClientRect();
      return button.top>=scroll.getBoundingClientRect().top && button.bottom<=composer.top;
    });
    if(!reachable) throw new Error('Proposal action hidden by composer');
    await page.evaluate(()=>{document.querySelector('#chatScroll').scrollTop=0;});
    await page.screenshot({path:`output/playwright/proposal-${size.width}.png`});
  }
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole('button',{name:'切换外观'}).click();await privacy();
  await page.screenshot({path:'output/playwright/proposal-dark.png'});
  await page.setViewportSize({width:1440,height:1000});
  await choose(1,'current');await page.getByRole('button',{name:'一次提交全部选择'}).click();
  await page.getByText('选择已记录，尚未发布').waitFor();
  await page.reload();await page.getByText('选择已记录，尚未发布').waitFor();
  if(await page.getByRole('button',{name:'一次提交全部选择'}).count()) throw new Error('Final assessment editable');
  await page.getByRole('button',{name:'新建界面会话'}).click();
  if(await page.locator('.proposal-review').count()) throw new Error('Proposal moved across display sessions');
  await page.locator('[data-session-id]').nth(1).click();
  await page.getByText('选择已记录，尚未发布').waitFor();
  await page.getByRole('button',{name:'取消评估'}).click();await page.getByText('评估已取消').waitFor();
  await page.locator('[data-delete-session-id]').nth(1).click();
  await page.waitForFunction(()=>document.querySelectorAll('[data-session-id]').length===1);
  await page.reload();
  await page.waitForFunction(()=>!document.querySelector('#proposalUpload').disabled);
  if(await page.locator('.proposal-review').count()) throw new Error('Deleted assessment restored');
  await upload('many');await page.getByRole('button',{name:'一次提交全部选择'}).waitFor();
  if(await page.locator('.proposal-field').count()!==14) throw new Error('Dynamic decisions truncated');
  await page.getByRole('button',{name:'清空当前显示'}).click();
  await page.waitForFunction(()=>!document.querySelector('.proposal-review'));
  await upload('unsafe');await page.getByText('评估响应不完整，未展示或提交任何选择。').waitFor();
  if(await page.locator('.proposal-field').count()) throw new Error('Unsafe partial review rendered');
  await privacy();
  const flags=await page.evaluate(()=>window.proposalFixtureFlags);
  if(flags.queryCalls || flags.selections!==2 || !flags.closes || !flags.resumes) throw new Error(JSON.stringify(flags));
  if(errors.length) throw new Error(errors.join('\n'));
  return {allChoices:true,refresh:true,defer:true,finalImmutable:true,cancel:true,delete:true,clear:true,isolation:true,dynamic14:true,privacy:true,desktop:true,mobile:true,dark:true,noQuery:true};
}
