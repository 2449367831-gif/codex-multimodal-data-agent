async (page) => {
  const control=async(settings)=>page.evaluate(async(value)=>fetch("/v1/fixture",{method:"POST",body:JSON.stringify(value)}).then(r=>r.json()),settings);
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole("textbox",{name:"输入取数问题"}).fill("查询昨天成品销售出库数量Top20");
  await page.getByRole("button",{name:"提交取数问题"}).click();
  await page.waitForSelector('[data-drilldown-sku="TEST-A"]');
  await control({dropNextPost:true});
  await page.locator('[data-drilldown-sku="TEST-A"]').focus(); await page.keyboard.press("Enter");
  await page.waitForSelector(".drilldown-panel .business-table");
  if((await control({})).detailPosts!==1) throw new Error("Lost POST response duplicated task");
  if(!await page.locator(".drilldown-heading h3").evaluate(el=>el===document.activeElement)) throw new Error("Async completion lost focus");
  await page.getByRole("button",{name:"切换外观",exact:true}).click();
  await page.screenshot({path:"output/playwright/sales-drilldown-dark.png"});
  // Corrupt only the synthetic persisted page, never real user history.
  await page.evaluate(()=>{
    for(const key of Object.keys(localStorage).filter(k=>k.startsWith("royal-little-tiger-query-history-v1:"))) {
      const data=JSON.parse(localStorage.getItem(key));
      for(const session of data.sessions) for(const event of session.events) {
        if(event.drilldown?.table) event.drilldown.table.rows[0].sku_code="WRONG-SKU";
      }
      localStorage.setItem(key,JSON.stringify(data));
    }
  });
  await page.reload(); await page.waitForSelector(".drilldown-error");
  if(await page.locator(".drilldown-panel .business-table").count()) throw new Error("Corrupt stored detail accepted");
  await page.evaluate(()=>{
    for(const key of Object.keys(localStorage).filter(k=>k.startsWith("royal-little-tiger-query-history-v1:"))) {
      const data=JSON.parse(localStorage.getItem(key));
      for(const session of data.sessions) for(const event of session.events) {
        if(event.businessTable?.drilldown) event.businessTable.drilldown.expires_at=1;
        event.drilldown=null;
      }
      localStorage.setItem(key,JSON.stringify(data));
    }
  });
  await page.reload(); await page.waitForSelector('[data-drilldown-sku="TEST-B"]');
  const before=(await control({})).detailPosts;
  await page.locator('[data-drilldown-sku="TEST-B"]').click();
  await page.waitForSelector(".drilldown-error");
  if((await control({})).detailPosts!==before || !await page.locator(".drilldown-error").innerText().then(t=>t.includes("过期"))) throw new Error("Expired ranking caused query");
  return {lostResponseSameReceipt:true,completionFocus:true,corruptHistoryRejected:true,expiredBeforeQuery:true,darkModeRendered:true};
}
