async (page) => {
  // Requires the synthetic API fixture; no real business rows are captured.
  await page.getByRole("textbox", { name: "输入取数问题" }).fill("查询昨天各仓库实际销售出库数量和出库单数");
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.waitForFunction(() => document.querySelector(".business-result-notes"));
  await page.reload();
  await page.waitForFunction(() => document.querySelector(".business-result-notes"));
  const checks = await page.evaluate(() => {
    const tables = [...document.querySelectorAll(".business-table")];
    const table = tables.at(-1);
    return {
      completeRows: table.querySelectorAll("tbody tr").length === 120,
      countHeading: table.innerText.includes("出库单数"),
      notesRestored: document.body.innerText.includes("零数量的有效出库单仍计入单数"),
      noChoices: !document.querySelector(".clarification-card")
    };
  });
  if (Object.values(checks).some((value) => !value)) throw new Error(JSON.stringify(checks));
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 375, height: 812 }]) {
    await page.setViewportSize(viewport);
    await page.locator(".business-result-table").last().evaluate((element) => element.scrollIntoView({ block: "start" }));
    const frame = page.locator(".business-table-scroll").last();
    await frame.evaluate((element) => { element.scrollTop = 0; element.scrollLeft = 0; });
    const layout = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
    if (!layout) throw new Error("table causes page-level overflow");
    await page.screenshot({ path: `output/playwright/sales-measures-${viewport.width}.png` });
    await frame.evaluate((element) => { element.scrollTop = element.scrollHeight; element.scrollLeft = element.scrollWidth; });
    const endVisible = await frame.evaluate((element) => element.scrollHeight - element.scrollTop <= element.clientHeight + 1);
    if (!endVisible) throw new Error("last aggregate row is unreachable");
  }
  return checks;
}
