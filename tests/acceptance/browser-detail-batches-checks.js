async (page) => {
  const check=(value,message)=>{if(!value)throw new Error(message);};
  await page.locator("#questionInput").fill("近14天实际销售出库明细");
  await page.getByRole("button",{name:"提交取数问题",exact:true}).click();
  await page.getByRole("button",{name:"取回第1批",exact:true}).waitFor();
  await page.getByRole("button",{name:"取回第1批",exact:true}).click();
  await page.getByRole("heading",{name:"实际销售出库明细 · 第1批",exact:true}).waitFor();
  check(await page.locator(".execution-card").getByText("本批已完成，明细尚未全部取回",{exact:true}).count()===1,"partial batch mislabeled");
  check(await page.getByRole("heading",{name:"实际销售出库范围汇总",exact:true}).count()===1,"summary lost");
  await page.getByRole("button",{name:"下一页",exact:true}).click();
  await page.getByRole("cell",{name:"TEST-OUT-3",exact:true}).waitFor();
  const downloadPromise=page.waitForEvent("download");
  await page.getByRole("button",{name:"下载本批完整明细",exact:true}).click();
  const download=await downloadPromise;
  const stream=await download.createReadStream();let csv="";for await(const chunk of stream)csv+=chunk.toString("utf8");
  check(csv.includes("TEST-OUT-1")&&csv.includes("TEST-OUT-3")&&csv.includes("'=FORMULA-TEST"),"download missed pages or formula escape");
  await page.setViewportSize({width:375,height:812});
  const next=page.getByRole("button",{name:"取回第2批",exact:true}).last();
  await next.scrollIntoViewIfNeeded();
  await next.focus();
  check(await next.evaluate(node=>{const rect=node.getBoundingClientRect();return rect.width>=44&&rect.height>=44&&rect.right<=innerWidth;}),"continuation button clipped or too small");
  await page.screenshot({path:"output/playwright/detail-batches-mobile-partial.png"});
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>fetch("/v1/fixture",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"pending"})}));
  await page.getByRole("button",{name:"取回第2批",exact:true}).last().click();
  await page.waitForFunction(()=>Object.keys(localStorage).some(key=>key.startsWith("royal-little-tiger-query-history-v1:")&&localStorage.getItem(key).includes('"detailBatch"')&&localStorage.getItem(key).includes('"taskId":"task_')));
  const before=await page.evaluate(()=>fetch("/v1/fixture").then(r=>r.json()));
  await page.reload();
  await page.evaluate(()=>fetch("/v1/fixture",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"success"})}));
  await page.getByRole("heading",{name:"实际销售出库明细 · 第2批",exact:true}).waitFor();
  const after=await page.evaluate(()=>fetch("/v1/fixture").then(r=>r.json()));
  check(before.posts===2&&after.posts===2&&after.gets>0,"reload duplicated task");
  check(await page.getByRole("button",{name:/取回第/}).count()===0,"completed chain still offers next batch");
  check(await page.getByRole("cell",{name:"TEST-OUT-5",exact:true}).count()===1,"last batch missing");
  for(const [width,height,name] of [[1440,1000,"desktop"],[375,812,"mobile"],[812,375,"landscape"]]){
    await page.setViewportSize({width,height});
    await page.locator(".detail-batch-controls").last().scrollIntoViewIfNeeded();
    check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),"horizontal viewport overflow");
    if(width<=900)check(await page.locator(".sidebar").evaluate(node=>getComputedStyle(node).visibility==="hidden"),"closed sidebar can cover results");
    const targets=await page.locator(".detail-batch-actions button").evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect();return {width:r.width,height:r.height};}));
    check(targets.every(r=>r.width>=44&&r.height>=44),"small touch target");
    await page.screenshot({path:`output/playwright/detail-batches-${name}.png`});
  }
  await page.emulateMedia({reducedMotion:"reduce"});
  await page.evaluate(()=>document.documentElement.dataset.theme="dark");
  await page.setViewportSize({width:375,height:812});
  await page.screenshot({path:"output/playwright/detail-batches-dark.png"});
  return {passed:true,posts:after.posts,recovered_without_duplicate:true,all_public_pages_downloaded:true};
}
