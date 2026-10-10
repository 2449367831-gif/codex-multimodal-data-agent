async (page) => {
  const check=(value,message)=>{if(!value)throw Error(message);};
  await page.locator('#questionInput').fill('近14天实际销售出库明细');
  await page.getByRole('button',{name:'提交取数问题',exact:true}).click();
  await page.getByRole('button',{name:'导出全部明细',exact:true}).click();
  await page.getByText('连接中断，已保存进度保留。服务恢复后可继续导出。',{exact:true}).waitFor();
  check(await page.getByText('文件已保存 2 / 5 行',{exact:true}).count()===1,'incorrect partial progress');
  check(await page.getByText('完整文件已保存',{exact:true}).count()===0,'partial file marked complete');
  await page.reload();
  await page.getByRole('button',{name:'继续导出全部',exact:true}).waitFor();
  await page.evaluate(()=>fetch('/v1/fixture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'success'})}));
  await page.getByRole('button',{name:'继续导出全部',exact:true}).click();
  await page.getByText('全部明细已保存，文件完整性校验通过',{exact:true}).waitFor();
  const results=await page.evaluate(async()=>{
    const file=await (await (await navigator.storage.getDirectory()).getFileHandle('synthetic-export.csv')).getFile();
    const csv=await file.text();
    const reads=(await (await fetch('/v1/fixture')).json()).reads;
    return {lines:csv.trim().split('\n').length,allRows:[1,2,3,4,5].every(n=>csv.includes(`TEST-OUT-${n}`)),reads};
  });
  check(results.lines===6&&results.allRows,'incomplete or duplicated export');
  check(results.reads.join() === '0,1,1,2','refresh did not resume from committed checkpoint');
  for(const [width,height,name] of [[1440,1000,'desktop'],[375,812,'mobile'],[812,375,'landscape']]) {
    await page.setViewportSize({width,height});
    await page.locator('.detail-export-controls').scrollIntoViewIfNeeded();
    check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'viewport overflow');
    check(await page.locator('[data-full-export]').evaluate(node=>{const r=node.getBoundingClientRect();return r.width>=44&&r.height>=44&&r.right<=innerWidth;}),'export control inaccessible');
    await page.screenshot({path:`output/playwright/detail-export-${name}.png`});
  }
  await page.setViewportSize({width:375,height:812});
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.evaluate(()=>document.documentElement.dataset.theme='dark');
  await page.screenshot({path:'output/playwright/detail-export-dark.png'});
  await page.evaluate(async()=>{const handle=await(await navigator.storage.getDirectory()).getFileHandle('synthetic-export.csv');
    const writer=await handle.createWritable({keepExistingData:true});await writer.seek(0);await writer.write('changed');await writer.close();});
  await page.getByRole('button',{name:'核验已保存文件',exact:true}).click();
  await page.getByText('文件内容与已保存进度不一致，已停止续写。原文件未被覆盖。',{exact:true}).waitFor();
  check(await page.getByText('完整文件已保存',{exact:true}).count()===0,'modified completed file still marked complete');
  await page.getByRole('button',{name:'重新导出到新文件',exact:true}).waitFor();
  return {passed:true,...results};
}
