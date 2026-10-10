async (page) => {
  const errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  const control=async(settings)=>page.evaluate(async(value)=>fetch("/v1/fixture",{method:"POST",body:JSON.stringify(value)}).then(r=>r.json()),settings);
  const clickSku=(sku)=>page.locator(`[data-drilldown-sku="${sku}"]`).last().click();
  await page.emulateMedia({reducedMotion:"reduce"});
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole("textbox",{name:"输入取数问题"}).fill("查询昨天成品销售出库数量Top20");
  await page.getByRole("button",{name:"提交取数问题"}).click();
  await page.waitForSelector('[data-drilldown-sku="TEST-A"]',{timeout:6000});
  const ranking=await page.locator(".business-table-ranking table").innerText();
  await page.locator('[data-drilldown-sku="TEST-A"]').focus();
  await page.keyboard.press("Enter");
  await page.waitForSelector(".drilldown-panel .business-table");
  if(!await page.locator(".drilldown-panel").innerText().then(t=>t.includes("9,007,199,254,740,993.25")&&t.includes("暂无")&&t.includes("000012345678901234567890"))) throw new Error("Detail precision/null/code lost");
  await page.getByRole("button",{name:"下一页",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".drilldown-panel").textContent.includes("TEST-OUT-3"));
  await page.reload();
  await page.waitForSelector(".drilldown-panel .business-table");
  if(!await page.locator(".drilldown-panel").innerText().then(t=>t.includes("TEST-OUT-3"))) throw new Error("Refresh lost detail page");
  if((await control({})).detailPosts!==1) throw new Error("Refresh queried again");
  await control({pageFailure:true});
  await page.getByRole("button",{name:"上一页",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".result-page-status").textContent.includes("过期"));
  if(!await page.locator(".drilldown-panel").innerText().then(t=>t.includes("TEST-OUT-3"))) throw new Error("Page failure lost prior data");
  const layout=async()=>{
    await page.waitForFunction(()=>[...document.images].filter(i=>i.getBoundingClientRect().width).every(i=>i.complete&&i.naturalWidth));
    if(!await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth&&[...document.querySelectorAll("[data-drilldown-sku]")].every(b=>b.getBoundingClientRect().height>=44))) throw new Error("Drilldown layout overflow/target too small");
  };
  await layout();
  await page.screenshot({path:"output/playwright/sales-drilldown-desktop.png"});
  await page.setViewportSize({width:375,height:812}); await layout();
  await page.screenshot({path:"output/playwright/sales-drilldown-mobile.png"});
  await page.setViewportSize({width:812,height:375}); await layout();
  await page.getByRole("button",{name:"返回排名",exact:true}).click();
  if(await page.locator(".drilldown-panel").count()) throw new Error("Return did not close detail");
  if(!await page.locator('[data-drilldown-sku="TEST-A"]').evaluate(el=>el===document.activeElement)) throw new Error("Return lost focus");
  for(const mode of ["wrong-source","wrong-sku","wrong-total","bad-receipt","failed","expired"]) {
    await control({mode}); await clickSku("TEST-B");
    await page.waitForSelector(".drilldown-error");
    if(await page.locator(".drilldown-panel .business-table").count()) throw new Error("Invalid detail displayed");
    if(await page.locator(".business-table-ranking table").innerText()!==ranking) throw new Error("Failure changed ranking");
    if(!await page.locator(".user-question").count() && !await page.getByText("查询昨天成品销售出库数量Top20",{exact:true}).count()) throw new Error("Question disappeared");
    await page.getByRole("button",{name:"返回排名",exact:true}).click();
  }
  await control({mode:"pending",pageFailure:false}); await clickSku("TEST-B");
  await page.waitForFunction(()=>Object.values(localStorage).some(raw=>raw.includes('"taskId":"task_')&&raw.includes('"skuCode":"TEST-B"')));
  const before=(await control({})).detailPosts;
  await page.reload();
  await page.waitForSelector('.drilldown-panel[aria-busy="true"]');
  await control({mode:"success"});
  await page.waitForSelector(".drilldown-panel .business-table");
  if((await control({})).detailPosts!==before) throw new Error("Resume created duplicate task");
  if(!await page.locator(".drilldown-panel").innerText().then(t=>t.includes("TEST-B"))) throw new Error("Resume lost selected SKU");
  if(errors.length) throw new Error(errors.join("\n"));
  return {click:true,keyboard:true,exactDecimal:true,paging:true,refresh:true,expiry:true,invalidSource:true,invalidSku:true,invalidTotal:true,invalidReceipt:true,failedKeepsRanking:true,resumeSameTask:true,desktop:true,mobile:true,landscape:true};
}
