async (page) => {
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole('textbox',{name:'输入取数问题'}).fill('查询DMS2026年1至8月积分订单，按月统计订单数并返回客户名称、客户编码和订单号明细');
  await page.getByRole('button',{name:'提交取数问题'}).click();
  await page.waitForSelector('.business-table-orders');
  if(await page.locator('.business-table').count()!==2) throw new Error('Expected both tables');
  const monthly=await page.locator('.business-table-months').innerText();
  if(!await page.locator('.business-table-orders').innerText().then(text=>text.includes('000012345678901234567890')&&text.includes('暂无'))) throw new Error('Lost nulls or code precision');
  await page.getByRole('button',{name:'下一页',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.business-table-orders').textContent.includes('TEST-ORDER-003'));
  if(await page.locator('.business-table-months').innerText()!==monthly) throw new Error('Detail pagination changed summary');
  await page.reload();
  await page.waitForSelector('.business-table-orders');
  if(await page.locator('.business-table').count()!==2 || !await page.locator('.business-table-orders').innerText().then(text=>text.includes('TEST-ORDER-003'))) throw new Error('Refresh lost multi-table current page');
  const layout=async()=>{
    await page.waitForFunction(()=>!document.getAnimations().some(a=>a.playState==='running'));
    await page.waitForFunction(()=>[...document.images].filter(i=>i.getBoundingClientRect().width).every(i=>i.complete&&i.naturalWidth));
    const good=await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth
      && [...document.querySelectorAll('.result-page-controls button,.result-page-controls input')].every(el=>el.getBoundingClientRect().height>=44)
      && document.querySelectorAll('.business-table-months tbody tr').length===2
      && document.querySelectorAll('.business-table-orders tbody tr').length===1);
    if(!good) throw new Error('Layout or table content failed');
  };
  await layout();
  await page.screenshot({path:'output/playwright/dms-orders-desktop.png'});
  await page.setViewportSize({width:375,height:812});
  await layout();
  await page.screenshot({path:'output/playwright/dms-orders-mobile.png'});
  await page.setViewportSize({width:812,height:375});
  await layout();
  await page.evaluate(()=>{window.dmsPageFailure=true;});
  await page.getByRole('button',{name:'上一页',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.result-page-status').textContent.includes('过期'));
  if(!await page.locator('.business-table-orders').innerText().then(text=>text.includes('TEST-ORDER-003'))) throw new Error('Expired fetch discarded current page');
  await page.evaluate(()=>{window.dmsInvalidTable=true;});
  await page.getByRole('textbox',{name:'输入取数问题'}).fill('查询DMS积分订单数和明细');
  await page.getByRole('button',{name:'提交取数问题'}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.execution-message').length===2 && !document.querySelector('.execution-message:last-child').textContent.includes('正在处理请求'));
  const last=page.locator('.execution-message').last();
  if(await last.locator('.business-table').count()!==0 || !await last.innerText().then(text=>text.includes('未完成'))) throw new Error('Invalid detail produced partial success');
  if(errors.length) throw new Error(errors.join('\n'));
  return {twoTables:true,independentPaging:true,refresh:true,nullsAndCodes:true,expired:true,invalidWholeResult:true,desktop:true,mobile:true,landscape:true};
}
