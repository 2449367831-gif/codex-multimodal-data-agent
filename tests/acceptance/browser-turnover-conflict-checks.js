async page => {
  const errors = [];
  let submissions = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.url().endsWith('/v1/tasks')) submissions++; });
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.setViewportSize({width:1440,height:1000});
  const question = '库存周转有两个历史说法，请一次确认全部口径。';
  await page.getByRole('textbox',{name:'输入取数问题'}).fill(question);
  await page.getByRole('button',{name:'提交取数问题'}).click();
  await page.locator('[data-option-id="tc_a"]').waitFor();
  if (await page.locator('.clarification-card fieldset').count() !== 6) throw new Error('missing decisions');
  if (await page.locator('input:checked').count()) throw new Error('guessed defaults');
  if (await page.locator('input[type=date]:visible').count()) throw new Error('unconditional dates');
  for (const option of ['tc_a','tc_realtime','tc_actual','tc_keep_returns','tc_all','tc_custom']) {
    await page.locator(`[data-option-id="${option}"]`).check();
  }
  await page.getByRole('button',{name:'确认并继续'}).click();
  await page.getByText('请选择有效的起止日期，结束日期必须晚于开始日期（结束日不计入）。').waitFor();
  if (submissions !== 1) throw new Error('invalid empty dates submitted');
  await page.getByLabel('开始日期（含）').fill('2026-09-01');
  await page.getByLabel('结束日期（不含）').fill('2026-08-01');
  await page.getByRole('button',{name:'确认并继续'}).click();
  if (submissions !== 1) throw new Error('reversed dates submitted');
  if (!await page.getByLabel('结束日期（不含）').evaluate(el => el === document.activeElement && el.getAttribute('aria-invalid') === 'true')) throw new Error('error focus missing');
  await page.getByLabel('开始日期（含）').fill('2026-08-01');
  await page.getByLabel('结束日期（不含）').fill('2026-09-01');
  await page.reload();
  await page.locator('[data-option-id="tc_custom"]:checked').waitFor();
  if (await page.locator('.clarification-card input:checked').count() !== 6) throw new Error('draft choices lost');
  if (await page.getByLabel('开始日期（含）').inputValue() !== '2026-08-01' || await page.getByLabel('结束日期（不含）').inputValue() !== '2026-09-01') throw new Error('draft dates lost');
  for (const size of [{width:1440,height:1000},{width:375,height:812},{width:812,height:375}]) {
    await page.setViewportSize(size);
    await page.getByRole('button',{name:'确认并继续'}).scrollIntoViewIfNeeded();
    const layout = await page.evaluate(() => ({
      fits: document.documentElement.scrollWidth <= innerWidth && [...document.querySelectorAll('.choice-option,.clarification-date')].every(el => el.scrollWidth <= el.clientWidth),
      targets: [...document.querySelectorAll('.clarification-date input')].every(el => el.getBoundingClientRect().height >= 44),
      assets: [...document.images].every(img => img.complete && img.naturalWidth > 0)
    }));
    if (Object.values(layout).some(value => !value)) throw new Error('layout/asset failure: '+JSON.stringify(layout));
    await page.screenshot({path:`output/playwright/turnover-conflict-${size.width}.png`});
  }
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#themeToggle').click();
  await page.screenshot({path:'output/playwright/turnover-conflict-dark.png'});
  await page.getByRole('button',{name:'确认并继续'}).click();
  await page.getByText(/无需重复选择/).waitFor();
  if (submissions !== 2 || await page.locator('.business-table,.clarification-card,.retry-button').count()) throw new Error('failed to close exactly once');
  await page.reload();
  await page.getByText(/无需重复选择/).waitFor();
  if (!await page.getByText(question,{exact:true}).count()) throw new Error('question lost');
  await page.getByRole('textbox',{name:'输入取数问题'}).fill(question);
  await page.getByRole('button',{name:'提交取数问题'}).click();
  await page.locator('[data-option-id="tc_a"]').check();
  await page.evaluate(() => {
    const key = Object.keys(localStorage).find(key => key.endsWith(':c_conflict_fixture'));
    const saved = JSON.parse(localStorage.getItem(key));
    for (const session of saved.sessions) {
      for (const event of session.events) {
        if (event.kind === 'clarification') event.clarification.draft_id = 'cd_'+'9'.repeat(64);
      }
    }
    localStorage.setItem(key, JSON.stringify(saved));
  });
  await page.reload();
  await page.locator('[data-option-id="tc_a"]').waitFor();
  if (await page.locator('.clarification-card input:checked').count()) throw new Error('foreign draft restored');
  if (errors.length) throw new Error(errors.join('\n'));
  return {allSixDecisions:true,dateValidation:true,draftReload:true,draftMismatchRejected:true,terminalReload:true,noBusinessQuery:true,desktop:true,mobile:true,landscape:true,reducedMotion:true,darkMode:true};
}
