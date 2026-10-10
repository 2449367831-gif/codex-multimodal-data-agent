async (page) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const control = (mode) => page.evaluate((value) => fetch("/v1/fixture", {method: "POST", body: JSON.stringify({mode: value})}).then((r) => r.json()), mode);
  const submit = async () => {
    await page.getByRole("textbox", {name: "输入取数问题"}).fill("查询 DMS 销售订单的日期、省区、城市经理、客户编码、客户名称和订单数。城市经理通过 `t_sales_order.owner_manager = t_employee.employee_id` 关联，展示 `t_employee.actual_name`；客户名称直接取 `t_sales_order.customer_name`。");
    await page.getByRole("button", {name: "提交取数问题"}).click();
    await page.waitForSelector(".business-table", {timeout: 6000});
  };
  await page.setViewportSize({width: 1440, height: 1000});
  await page.emulateMedia({reducedMotion: "reduce"});
  await submit();
  if (await page.locator(".business-table th").count() !== 9 || await page.locator(".business-table tbody tr").count() !== 100) throw new Error("Missing columns or rows");
  const originalSummary = await page.locator(".business-result-summary").innerText();
  const originalNotes = await page.locator(".business-result-notes").innerText();
  if (!(await page.locator(".business-table").innerText()).includes("9223372036854775807") || !(await page.locator(".business-table").innerText()).includes("0000000000000000000000")) throw new Error("Identifiers lost precision");
  const missing = page.locator(".business-table tbody tr").nth(1);
  if (await missing.locator("td").filter({hasText: /^暂无$/}).count() !== 5 || !(await missing.innerText()).includes("未配置经理")) throw new Error("Missing attributes were hidden or changed to zero");
  await page.screenshot({path: "output/playwright/dms-manager-missing.png"});
  for (const mode of ["changed-summary", "changed-caption", "changed-notes", "changed-expiry", "unsafe-row", "unknown-column"]) {
    await control(mode);
    await page.getByRole("button", {name: "下一页", exact: true}).click();
    await page.waitForFunction(() => document.querySelector(".result-page-status").textContent.includes("校验失败"), null, {timeout: 6000});
    if (await page.locator(".business-result-summary").innerText() !== originalSummary || await page.locator(".business-result-notes").innerText() !== originalNotes || await page.getByRole("spinbutton", {name: "结果页码"}).inputValue() !== "1") throw new Error("Inconsistent page replaced report");
    if ((await page.locator("body").innerText()).includes("PRIVATE_DIAGNOSTIC")) throw new Error("Rejected page text leaked");
  }
  await control("success");
  await page.getByRole("button", {name: "下一页", exact: true}).click();
  await page.waitForFunction(() => document.querySelectorAll(".business-table tbody tr").length === 5);
  if (await page.locator(".business-result-summary").innerText() !== originalSummary) throw new Error("Page changed full-scope total");
  await page.reload();
  await page.waitForFunction(() => document.querySelectorAll(".business-table tbody tr").length === 5);
  const layout = async () => {
    await page.waitForFunction(() => !document.getAnimations().some((a) => a.playState === "running"));
    await page.waitForFunction(() => [...document.images].filter((i) => i.getBoundingClientRect().width).every((i) => i.complete && i.naturalWidth));
    const flags = await page.evaluate(() => ({
      noOverflow: document.documentElement.scrollWidth <= innerWidth,
      targets: [...document.querySelectorAll(".result-page-controls button,.result-page-controls input")].every((el) => el.getBoundingClientRect().height >= 44),
      readableIdentifiers: getComputedStyle(document.querySelector(".table-column-manager_key")).whiteSpace === "nowrap",
      fullSummaryLabels: [...document.querySelectorAll(".business-summary-item span")].every((el) => el.scrollWidth <= el.clientWidth),
      noSql: !/SELECT |t_sales_order|t_employee/.test(document.querySelector(".business-result").textContent)
    }));
    if (Object.values(flags).some((value) => !value)) throw new Error(JSON.stringify(flags));
  };
  for (const size of [{width: 1440, height: 1000}, {width: 375, height: 812}, {width: 812, height: 375}]) {
    await page.setViewportSize(size);
    await layout();
    await page.screenshot({path: `output/playwright/dms-manager-${size.width}.png`});
  }
  await page.getByRole("button", {name: "切换外观"}).click();
  await layout();
  await page.screenshot({path: "output/playwright/dms-manager-dark.png"});
  await control("expired");
  await page.getByRole("button", {name: "上一页", exact: true}).click();
  await page.waitForFunction(() => document.querySelector(".result-page-status").textContent.includes("过期"));
  if (await page.locator(".business-table tbody tr").count() !== 5) throw new Error("Expiry discarded visible rows");
  if (errors.length) throw new Error(errors.join("\n"));
  return {allColumns:true,exactCodes:true,consistentMetadata:true,refresh:true,expiry:true,desktop:true,mobile:true,landscape:true,dark:true};
}
