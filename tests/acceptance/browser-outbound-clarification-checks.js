async page => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  for (const runnable of [true, false]) {
    await page.evaluate(()=>localStorage.clear());
    await page.reload();
    await page.getByRole('textbox',{name:'输入取数问题'}).fill('昨天出库量');
    await page.getByRole('button',{name:'提交取数问题'}).click();
    await page.getByRole('radio',{name:'销售出库',exact:true}).waitFor();
    if (await page.getByRole('radio').count() !== 13) throw new Error('missing decisions');
    if (await page.locator('.clarification-card fieldset').count() !== 3) throw new Error('missing groups');
    await page.reload();
    await page.getByRole('radio',{name:'销售出库',exact:true}).waitFor();
    if (await page.getByRole('radio').count() !== 13) throw new Error('pending card lost on reload');
    await page.getByRole('radio',{name:runnable?'销售出库':'所有出库（销售与调拨）',exact:true}).check();
    await page.getByRole('radio',{name:runnable?'实发数量':'分配数量（单位待确认）',exact:true}).check();
    await page.getByRole('radio',{name:runnable?'实际出库时间':'审核时间（暂未接入）',exact:true}).check();
    for (const size of [{width:1440,height:1000},{width:375,height:812},{width:812,height:375}]) {
      await page.setViewportSize(size);
      await page.getByRole('button',{name:'确认并继续'}).scrollIntoViewIfNeeded();
      const fit = await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth && [...document.querySelectorAll('.choice-option')].every(el=>el.scrollWidth<=el.clientWidth));
      if (!fit) throw new Error('choices overflow');
      await page.screenshot({path:`output/playwright/outbound-choices-${runnable}-${size.width}.png`});
    }
    await page.getByRole('button',{name:'确认并继续'}).click();
    if (runnable) await page.waitForSelector('.business-table');
    else await page.getByText(/无需重复选择/).waitFor();
    await page.reload();
    if (runnable) await page.waitForSelector('.business-table');
    else await page.getByText(/无需重复选择/).waitFor();
    if (!await page.getByText('昨天出库量',{exact:true}).count()) throw new Error('question lost');
    if (!runnable && await page.locator('.business-table').count()) throw new Error('fabricated table');
    if (!runnable && await page.locator('.retry-button').count()) throw new Error('pointless retry');
    if (!runnable && !(await page.locator('body').innerText()).includes('口径待确认，未执行查询')) throw new Error('wrong outcome label');
    if ((await page.locator('body').innerText()).includes('该选项')) throw new Error('placeholder choices');
    await page.screenshot({path:`output/playwright/outbound-result-${runnable}.png`});
  }
  if (errors.length) throw new Error(errors.join('\n'));
  return {allDecisions:true,pendingCardReload:true,resultReload:true,desktop:true,mobile:true,landscape:true};
}
