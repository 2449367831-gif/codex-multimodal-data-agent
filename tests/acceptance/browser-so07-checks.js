async page => {
  const errors = [];
  page.on('pageerror',error=>errors.push(error.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  const submit = async text => {
    await page.getByRole('textbox',{name:'输入取数问题'}).fill(text);
    await page.getByRole('button',{name:'提交取数问题'}).click();
  };
  const layout = async () => {
    await page.waitForFunction(()=>!document.getAnimations().some(a=>a.playState==='running'));
    await page.waitForFunction(()=>[...document.images].filter(i=>i.getBoundingClientRect().width).every(i=>i.complete&&i.naturalWidth));
    if(!await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)) throw Error('Page overflow');
    if(!await page.locator('.business-table-scroll').first().evaluate(el=>el.scrollWidth>el.clientWidth)) throw Error('Wide table not scrollable');
  };
  await page.setViewportSize({width:1440,height:1000});
  await submit('查询今天SO07社团订单，展示原始请求数量和源单位代码');
  await page.waitForSelector('td.table-column-request_quantity');
  if(await page.locator('.business-table th').count()!==14) throw Error('SO07 fields missing');
  if(await page.locator('td.table-column-request_quantity').innerText()!=='123,456,789,012,345,678,901,234,567,890.00') throw Error('Quantity display format mismatch');
  if(await page.locator('td.table-column-quantity_unit_reference').innerText()!=='010') throw Error('Unit code lost');
  if(await page.locator('td.table-column-sales_unit_reference').innerText()!=='040') throw Error('Sales unit code lost');
  if(!await page.locator('td.table-column-cargo_owner').evaluate(el=>getComputedStyle(el).whiteSpace==='nowrap')) throw Error('Owner code wraps');
  const summary = await page.locator('.business-summary-item').innerText();
  if(!summary.includes('订单数') || summary.includes('箱') || summary.includes('合计')) throw Error('Converted or totaled quantity');
  await layout();
  await page.screenshot({path:'output/playwright/so07-desktop.png'});
  await page.setViewportSize({width:375,height:812});
  await layout();
  await page.locator('.business-table-scroll').evaluate(el=>el.scrollLeft=el.scrollWidth);
  await page.screenshot({path:'output/playwright/so07-mobile.png'});
  await page.getByRole('button',{name:'下一页',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('td.table-column-sales_order_code')?.textContent==='TEST-EMPTY');
  if(await page.locator('td.table-column-request_quantity').innerText()!=='暂无') throw Error('Missing quantity became zero');
  await page.reload();
  await page.waitForSelector('td.table-column-request_quantity');
  if(await page.locator('td.table-column-sales_order_code').innerText()!=='TEST-EMPTY') throw Error('Refresh lost page');
  await page.setViewportSize({width:812,height:375});
  await layout();
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#themeToggle').click();
  await layout();
  await page.screenshot({path:'output/playwright/so07-dark.png'});
  await page.evaluate(()=>window.so07PageExpired=true);
  await page.getByRole('button',{name:'上一页',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.result-page-status')?.textContent.includes('过期'));
  if(await page.locator('td.table-column-request_quantity').innerText()!=='暂无') throw Error('Expiry discarded current page');
  await page.evaluate(()=>window.so07Invalid=true);
  await submit('查询另一组模拟SO07订单');
  await page.waitForFunction(()=>document.querySelectorAll('.execution-message').length===2 && !document.querySelector('.execution-message:last-child').textContent.includes('正在处理请求'));
  if(await page.locator('.execution-message').last().locator('.business-table').count()) throw Error('Cross-unit summary leaked');
  if(errors.length) throw Error(errors.join('\n'));
  return {rawPrecision:true,unitCodes:true,noTotals:true,nulls:true,paging:true,refresh:true,
    expiredPageRetained:true,invalidRejected:true,desktop:true,mobile:true,landscape:true,dark:true};
}
