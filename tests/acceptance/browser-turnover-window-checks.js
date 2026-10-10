async page => {
  const errors = [];
  await page.emulateMedia({reducedMotion:'reduce'});
  page.on('pageerror', error => errors.push(error.message));
  const question = '生成中心仓和分仓14日周转数据图片';
  await page.getByRole('textbox',{name:'输入取数问题'}).fill(question);
  await page.getByRole('button',{name:'提交取数问题'}).click();
  await page.locator('.message-error').filter({hasText:'查询执行超时，未返回完整结果'}).waitFor();
  if (!await page.getByText(question,{exact:true}).count()) throw new Error('question hidden after timeout');
  await page.getByRole('button',{name:'重试本次问题'}).click();
  await page.locator('.report-artifact img').waitFor();
  await page.waitForFunction(() => [...document.querySelectorAll('.report-artifact img')].every(image => image.complete && image.naturalWidth > 1000));
  for (let reload = 0; reload < 2; reload++) {
    await page.reload();
    await page.locator('.report-artifact img').waitFor();
    if (!await page.getByText(question,{exact:true}).count()) throw new Error('question lost on refresh');
  }
  for (const width of [1440,375]) {
    await page.setViewportSize({width,height:900});
    await page.locator('.report-artifact').scrollIntoViewIfNeeded();
    await page.waitForFunction(() => [...document.images].every(image => image.complete && image.naturalWidth > 0));
    if (!await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)) throw new Error('horizontal page overflow');
    await page.screenshot({path:`output/playwright/turnover-window-${width}.png`});
  }
  if (errors.length) throw new Error(errors.join('\n'));
  return {timeoutReason:true,retrySameQuestion:true,imageLoaded:true,refreshPreserved:true,desktop:true,mobile:true};
}
