async (page) => {
  await page.getByRole("textbox", { name: "输入取数问题" }).fill("合成测试：14天销售日历与5日移动平均");
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.locator(".business-table").last().waitFor();
  await page.reload();
  await page.locator(".business-table").last().waitFor();
  const checks = await page.evaluate(() => {
    const table = [...document.querySelectorAll(".business-table")].at(-1);
    return {
      completeCalendar: table.querySelectorAll("tbody tr").length === 14,
      windowLabel: table.innerText.includes("5日移动平均"),
      cumulativeLabel: table.innerText.includes("累计实发数量"),
      questionRestored: document.body.innerText.includes("合成测试：14天销售日历与5日移动平均"),
      notesRestored: document.body.innerText.includes("不足完整窗口为空"),
      noUnexpectedChoices: !document.querySelector(".clarification-card")
    };
  });
  if (Object.values(checks).some(value => !value)) throw new Error(JSON.stringify(checks));
  for (const width of [1440, 375]) {
    await page.setViewportSize({ width, height: 900 });
    await page.locator(".business-result-table").last().scrollIntoViewIfNeeded();
    if (!await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)) {
      throw new Error("page-level overflow");
    }
    await page.screenshot({ path: `output/playwright/timeseries-${width}.png` });
    const frame = page.locator(".business-table-scroll").last();
    await frame.evaluate(element => { element.scrollLeft = element.scrollWidth; element.scrollTop = element.scrollHeight; });
    if (!await frame.evaluate(element => element.scrollHeight - element.scrollTop <= element.clientHeight + 1
      && element.scrollWidth - element.scrollLeft <= element.clientWidth + 1)) {
      throw new Error("last calendar row or moving-average column is unreachable");
    }
  }
  return checks;
}
