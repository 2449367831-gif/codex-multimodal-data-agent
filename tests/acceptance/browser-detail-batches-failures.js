async (page) => {
  const check=(value,message)=>{if(!value)throw new Error(message);};
  await page.locator("#questionInput").fill("近14天实际销售出库明细");
  await page.getByRole("button",{name:"提交取数问题",exact:true}).click();
  await page.getByRole("button",{name:"取回第1批",exact:true}).waitFor();
  await page.evaluate(()=>fetch("/v1/fixture",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"wrong-source"})}));
  await page.getByRole("button",{name:"取回第1批",exact:true}).click();
  await page.getByText("查询结果校验未通过，未展示数据",{exact:true}).first().waitFor();
  check(await page.getByRole("heading",{name:"实际销售出库明细 · 第1批",exact:true}).count()===0,"foreign continuation displayed");
  check(await page.getByText("已取回 0 / 5 行",{exact:true}).count()===1,"failed response advanced progress");
  await page.evaluate(()=>fetch("/v1/fixture",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:"changed"})}));
  await page.getByRole("button",{name:"重试本次问题",exact:true}).last().click();
  await page.getByText("查询期间数据或口径已变化，请重新查询原问题；旧批次未继续拼接",{exact:true}).first().waitFor();
  check(await page.getByRole("button",{name:"重新查询原问题",exact:true}).count()===1,"changed source lacks recovery action");
  check(await page.getByRole("heading",{name:"实际销售出库范围汇总",exact:true}).count()===1,"failure removed summary");
  const stats=await page.evaluate(()=>fetch("/v1/fixture").then(r=>r.json()));
  check(stats.posts===2,"retry did not submit a fresh bounded batch");
  return {passed:true,foreign_result_rejected:true,source_change_keeps_original:true,retry_keeps_selection:true};
}
