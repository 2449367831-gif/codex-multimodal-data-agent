async page => {
  const errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  await page.emulateMedia({reducedMotion:"reduce"});
  await page.getByRole("textbox",{name:"输入取数问题"}).fill("查询昨天JYCK成品出库，按客户渠道汇总后换算箱数，四舍五入不保留小数");
  await page.getByRole("button",{name:"提交取数问题"}).click();
  await page.waitForSelector(".business-table");
  await page.reload();
  await page.waitForSelector(".business-table");
  for(const size of [{width:1440,height:1000},{width:375,height:812},{width:812,height:375}]) {
    await page.setViewportSize(size);
    const checks=await page.evaluate(()=>{
      const table=document.querySelector(".business-table");
      const cells=[...table.querySelectorAll("tbody tr")].map(row=>[...row.querySelectorAll("td")].map(cell=>cell.textContent.trim()));
      return {
        rows:cells.length===3,columns:table.querySelectorAll("th").length===11,
        rounded:cells[0][9]==="2箱",nullNotZero:!/^0(?:箱)?$/.test(cells[1][9]) && !/^0(?:箱)?$/.test(cells[2][9]),
        rawPrecision:cells[1][6].includes("4.25"),conflict:cells[2][10].includes("冲突"),
        noOverflow:document.documentElement.scrollWidth<=innerWidth,
        notes:document.querySelector(".business-result-notes").textContent.includes("空值不是零箱"),
        codesUnbroken:getComputedStyle(table.querySelector("td.table-column-sku_code")).whiteSpace==="nowrap",
        question:document.body.textContent.includes("查询昨天JYCK成品出库")
      };
    });
    if(Object.values(checks).some(value=>!value))throw new Error(JSON.stringify(checks));
    await page.locator(".business-table td").last().scrollIntoViewIfNeeded();
    await page.screenshot({path:`output/playwright/boxes-${size.width}.png`});
  }
  if(errors.length)throw new Error(errors.join("\n"));
  return {allColumns:true,rawPrecision:true,nulls:true,refresh:true,desktop:true,mobile:true,landscape:true};
}
