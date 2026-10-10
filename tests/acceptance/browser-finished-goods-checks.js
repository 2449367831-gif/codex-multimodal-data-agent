async (page) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.emulateMedia({ reducedMotion: "reduce" });
  const question = "昨天所有货主成品销售出库，保留SKU 5101010010008 和 5101010010009 两个例外，按SKU汇总";
  await page.getByRole("textbox", { name: "输入取数问题" }).fill(question);
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.waitForSelector(".business-result-notes");
  await page.reload();
  await page.waitForSelector(".business-result-notes");
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 375, height: 812 }, { width: 812, height: 375 }]) {
    await page.setViewportSize(viewport);
    await page.waitForFunction(() => !document.getAnimations().some(a => a.playState === "running"));
    const checks = await page.evaluate((asked) => ({
      rows: document.querySelectorAll(".business-table tbody tr").length === 3,
      columns: [...document.querySelectorAll(".business-table th")].filter(el => getComputedStyle(el).display !== "none").length === 3,
      codes: ["5101010010008", "5101010010009"].every(code => document.querySelector(".business-result-notes").textContent.includes(code)),
      question: document.body.textContent.includes(asked),
      noChoices: !document.querySelector(".clarification-card"),
      noOverflow: document.documentElement.scrollWidth <= innerWidth
    }), question);
    if (Object.values(checks).some(value => !value)) throw new Error(JSON.stringify(checks));
    await page.screenshot({ path: `output/playwright/finished-goods-${viewport.width}.png` });
  }
  if (errors.length) throw new Error(errors.join("\n"));
  return { fullSkuCodes: true, quantitiesAndCounts: true, exceptionsExplained: true, refresh: true, desktop: true, mobile: true, landscape: true };
}
