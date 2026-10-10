async (page) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("textbox", { name: "输入取数问题" }).fill("查询最近七天采购入库明细，按采购类型识别");
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.waitForSelector(".result-pagination");
  if (!await page.getByRole("button", { name: "上一页", exact: true }).isDisabled()) throw new Error("First-page boundary not disabled");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await page.waitForFunction(() => document.querySelector(".result-page-range").textContent.includes("3-4"));
  await page.reload();
  await page.waitForSelector(".result-pagination");
  if (!await page.locator(".business-table").innerText().then(s => s.includes("TEST-PAGE-2-LINE-1"))) throw new Error("Refreshed page lost");
  await page.getByRole("spinbutton", { name: "结果页码" }).fill("3");
  await page.getByRole("spinbutton", { name: "结果页码" }).press("Tab");
  await page.waitForFunction(() => document.querySelector(".result-page-range").textContent.includes("5-5"));
  if (!await page.getByRole("button", { name: "下一页", exact: true }).isDisabled()) throw new Error("Final page boundary not disabled");
  const check = async () => {
    await page.waitForFunction(() => !document.getAnimations().some(a => a.playState === "running") && (innerWidth >= 900 || document.querySelector(".sidebar").getBoundingClientRect().right <= 0));
    await page.waitForFunction(() => [...document.images].filter(i => i.getBoundingClientRect().width).every(i => i.complete && i.naturalWidth));
    const metrics = await page.evaluate(() => ({
      noOverflow: document.documentElement.scrollWidth <= innerWidth,
      targets: [...document.querySelectorAll(".result-page-controls button, .result-page-controls input")].every(el => el.getBoundingClientRect().height >= 44),
      oneRow: document.querySelectorAll(".business-table tbody tr").length === 1,
      correctTotal: document.querySelector(".business-table-count").textContent.includes("5 行"),
      fullColumns: document.querySelectorAll(".business-table th").length === 12
    }));
    if (Object.values(metrics).some(x => !x)) throw new Error(JSON.stringify(metrics));
  };
  await check();
  await page.screenshot({ path: "output/playwright/pages-desktop.png" });
  await page.setViewportSize({ width: 375, height: 812 });
  await check();
  await page.screenshot({ path: "output/playwright/pages-mobile.png" });
  await page.setViewportSize({ width: 812, height: 375 });
  await check();
  await page.evaluate(() => { window.receiptPageFailure = true; });
  await page.getByRole("button", { name: "上一页", exact: true }).click();
  await page.waitForFunction(() => document.querySelector(".result-page-status").textContent.includes("过期"));
  if (!await page.locator(".result-page-range").innerText().then(s => s.includes("5-5"))) throw new Error("Failed page request discarded current page");
  if (errors.length) throw new Error(errors.join("\n"));
  return { next: true, jump: true, refresh: true, expired: true, ownerScopedApi: "separate API test", desktop: true, mobile: true, landscape: true };
}
