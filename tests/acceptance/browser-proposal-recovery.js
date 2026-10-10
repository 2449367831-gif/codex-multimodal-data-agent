async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole('button',{name:'新建界面会话'}).click();
  await page.locator('#proposalFile').setInputFiles('tests/fixtures/proposal/expired.json');
  await page.getByRole('button',{name:'一次提交全部选择'}).waitFor();
  await page.reload();
  await page.getByText('此评估已过期或取消，请重新提交材料；未查询数据。').waitFor();
  if(await page.getByRole('button',{name:'一次提交全部选择'}).count()) throw new Error('Expired selection enabled');

  await page.getByRole('button',{name:'新建界面会话'}).click();
  await page.locator('#proposalFile').setInputFiles('tests/fixtures/proposal/lost.json');
  await page.getByRole('button',{name:'一次提交全部选择'}).waitFor();
  for(const field of await page.locator('.proposal-field').all()) await field.locator('input[value="current"]').check();
  await page.getByRole('button',{name:'一次提交全部选择'}).click();
  await page.getByText('连接中断，尚未确认评估状态。请恢复状态后继续；不会自动重复提交。').waitFor();
  if(await page.getByRole('button',{name:'一次提交全部选择'}).count()) throw new Error('Uncertain selection can resubmit');
  await page.getByRole('button',{name:'恢复评估状态'}).click();
  await page.getByText('选择已记录，尚未发布').waitFor();
  if(await page.getByRole('button',{name:'一次提交全部选择'}).count()) throw new Error('Accepted response loss caused resubmission');

  const closeFailure=route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":{"code":"unavailable"}}'});
  await page.route('**/v1/knowledge-proposal-sessions/close',closeFailure);
  const count=await page.locator('[data-session-id]').count();
  await page.locator('[data-delete-session-id]').first().click();
  await page.getByText('会话暂未删除：未能确认口径评估已关闭，请重试。').waitFor();
  if(await page.locator('[data-session-id]').count()!==count) throw new Error('Deleted before server closure');
  await page.unroute('**/v1/knowledge-proposal-sessions/close',closeFailure);

  await page.getByRole('button',{name:'新建界面会话'}).click();
  await page.locator('#proposalFile').setInputFiles('tests/fixtures/proposal/late.json');
  await page.waitForFunction(()=>document.querySelector('.proposal-review[aria-busy="true"]'));
  await page.locator('[data-delete-session-id]').first().click();
  await page.waitForTimeout(1000);
  if((await page.locator('body').innerText()).includes('口径待选择')) throw new Error('Late response revived deleted proposal');

  await page.getByRole('button',{name:'新建界面会话'}).click();
  await page.evaluate(()=>{
    const read=File.prototype.text;
    File.prototype.text=async function(){await new Promise(resolve=>setTimeout(resolve,600));return read.call(this);};
  });
  await page.locator('#proposalFile').setInputFiles('tests/fixtures/proposal/normal.json');
  await page.getByRole('button',{name:'新建界面会话'}).click();
  await page.waitForTimeout(900);
  if(await page.locator('.proposal-review').count()) throw new Error('Late file read crossed sessions');
  return {expiry:true,transportRecovery:true,closeFailureRetainsSession:true,lateResponseIgnored:true,lateFileIgnored:true};
}
