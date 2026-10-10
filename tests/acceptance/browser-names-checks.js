async page => {
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.getByRole('textbox',{name:'输入取数问题'}).fill('查询SKU TEST-SKU-1的出库数据，检查三处名称不一致');
  await page.getByRole('button',{name:'提交取数问题'}).click();
  await page.getByRole('radio',{name:'实际销售出库，核对名称'}).check();
  if(await page.getByRole('radio').count()!==2)throw new Error('unexpected clarification choices');
  await page.getByRole('button',{name:'确认并继续'}).click();
  await page.waitForSelector('.business-table');
  await page.reload();
  await page.waitForSelector('.business-table');
  for(const size of [{width:1440,height:1000},{width:375,height:812},{width:812,height:375}]) {
    await page.setViewportSize(size);
    const checks=await page.evaluate(()=>{
      const table=document.querySelector('.business-table');
      const rows=[...table.querySelectorAll('tbody tr')].map(r=>[...r.querySelectorAll('td')].map(c=>c.textContent.trim()));
      return {columns:table.querySelectorAll('th').length===12,rows:rows.length===2,
        precision:rows[0][3].includes('3.75'),zeroQuantityRetained:rows[1][3].includes('0.00'),
        nullNames:rows[1][5]==='暂无'&&rows[1][6]==='暂无',counts:rows[1][7]==='2'&&rows[1][8]==='1',
        conflict:rows[1][11].includes('名称不一致'),codeFallback:rows[1][2]==='TEST-SKU-2',
        notes:document.querySelectorAll('.business-result-notes li').length===3,
        question:document.body.textContent.includes('检查三处名称不一致'),
        noOverflow:document.documentElement.scrollWidth<=innerWidth};
    });
    if(Object.values(checks).some(v=>!v))throw new Error(JSON.stringify(checks));
    await page.locator('.business-table td').last().scrollIntoViewIfNeeded();
    await page.screenshot({path:`output/playwright/names-${size.width}.png`});
  }
  if(errors.length)throw new Error(errors.join('\n'));
  return {selection:true,refresh:true,twelveColumns:true,nulls:true,desktop:true,mobile:true,landscape:true};
}
