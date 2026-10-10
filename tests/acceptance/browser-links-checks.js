async page => {
  const errors = [];
  page.on('pageerror', error=>errors.push(error.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.getByRole('textbox',{name:'输入取数问题'}).fill('查询一张出库单的关联链路，区分来源单号与订单来源编码');
  await page.getByRole('button',{name:'提交取数问题'}).click();
  await page.getByRole('radio',{name:'查询范围内全部出库单',exact:true}).check();
  if (await page.getByRole('radio').count() !== 2) throw new Error('Unexpected link choices');
  await page.getByRole('button',{name:'确认并继续'}).click();
  await page.waitForSelector('.business-table');
  await page.reload();
  await page.waitForSelector('.business-table');
  for (const size of [{width:1440,height:1000},{width:375,height:812},{width:812,height:375}]) {
    await page.setViewportSize(size);
    const checks = await page.evaluate(()=>{
      const table = document.querySelector('.business-table');
      const rows = [...table.querySelectorAll('tbody tr')].map(row=>[...row.querySelectorAll('td')].map(cell=>cell.textContent.trim()));
      return {columns:table.querySelectorAll('th').length===11,rows:rows.length===3,
        sources:rows[0][4]==='TEST-HEADER-1'&&rows[0][5]==='TEST-LINE-001'&&rows[1][5]==='TEST-LINE-002',
        types:rows[0][7]==='JYCK'&&rows[0][8]==='SO01',
        missing:rows[2][3]==='暂无'&&rows[2][9]==='暂无'&&rows[2][10]==='未匹配销售订单',
        notes:document.querySelectorAll('.business-result-notes li').length===3,
        question:document.body.textContent.includes('区分来源单号与订单来源编码'),
        noPageOverflow:document.documentElement.scrollWidth<=innerWidth,
        noPrivate:!document.body.textContent.includes('header_variants')&&!document.body.textContent.includes('ywzt_ods')};
    });
    if (Object.values(checks).some(value=>!value)) throw new Error(JSON.stringify(checks));
    await page.locator('.business-table td').last().scrollIntoViewIfNeeded();
    await page.screenshot({path:`output/playwright/links-${size.width}.png`});
  }
  if (errors.length) throw new Error(errors.join('\n'));
  return {oneShotChoice:true,refresh:true,elevenColumns:true,nulls:true,multiSource:true,desktop:true,mobile:true,landscape:true};
}
