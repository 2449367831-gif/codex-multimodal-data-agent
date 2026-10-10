async (page) => {
  const control=async(settings)=>page.evaluate(async(value)=>fetch("/v1/fixture",{method:"POST",body:JSON.stringify(value)}).then(r=>r.json()),settings);
  const submit=async()=>{
    await page.getByRole("textbox",{name:"输入取数问题"}).fill("查询昨天成品销售出库数量Top20");
    await page.getByRole("button",{name:"提交取数问题"}).click();
  };
  await page.setViewportSize({width:1440,height:1000});
  await submit();
  await page.waitForSelector('[data-drilldown-sku="TEST-A"]');
  const original=await page.locator('[data-session-id][aria-current="page"]').getAttribute('data-session-id');
  await control({mode:"pending"});
  await page.locator('[data-drilldown-sku="TEST-A"]').click();
  await page.waitForFunction(()=>Object.values(localStorage).some(raw=>raw.includes('"taskId":"task_')&&raw.includes('"skuCode":"TEST-A"')));
  await page.getByRole("button",{name:"新建界面会话",exact:true}).click();
  const previous=(await control({mode:"success"})).detailGets;
  await page.waitForFunction(async(value)=>(await fetch('/v1/fixture').then(r=>r.json())).detailGets>value,previous);
  await page.waitForFunction(()=>Object.values(localStorage).some(raw=>raw.includes('"phase":"completed"')));
  if(await page.locator(".drilldown-panel").count()) throw new Error("Background detail leaked into new session");
  await page.locator(`[data-session-id="${original}"]`).click();
  await page.waitForSelector(".drilldown-panel .business-table");
  if(!await page.locator(".drilldown-panel").innerText().then(t=>t.includes("TEST-A"))) throw new Error("Original session lost result");
  if(await page.evaluate(()=>Object.values(localStorage).some(raw=>raw.includes("PRIVATE-CONTEXT-MARKER")))) throw new Error("Internal context persisted");
  await control({mode:"pending"});
  await page.locator('[data-drilldown-sku="TEST-B"]').click();
  await page.waitForFunction(()=>Object.values(localStorage).some(raw=>raw.includes('"skuCode":"TEST-B"')&&raw.includes('"phase":"loading"')));
  await page.locator(`[data-delete-session-id="${original}"]`).click();
  await control({mode:"success"});
  await page.reload();
  await page.waitForSelector('[data-session-id]');
  if(await page.locator(`[data-session-id="${original}"]`).count() || await page.locator(".drilldown-panel").count()) throw new Error("Deleted session reappeared");
  await control({mode:"invalid-ranking"}); await submit();
  await page.waitForFunction(()=>document.querySelector(".execution-message .execution-card-head strong")?.textContent.includes("未完成"),null,{timeout:6000});
  if(await page.locator("[data-drilldown-sku]").count() || !await page.locator(".execution-message").innerText().then(t=>t.includes("未完成"))) throw new Error("Invalid ranking reported success");
  return {backgroundOwnership:true,deletedSessionStaysDeleted:true,privateMetadataStripped:true,invalidRankingNotSuccess:true};
}
