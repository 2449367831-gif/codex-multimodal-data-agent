async (page) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("textbox", { name: "输入取数问题" }).fill("查询采购入库明细，采用入库类型为采购的口径");
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.waitForSelector(".business-table");
  const check = async () => {
    await page.waitForFunction(() => !document.getAnimations().some((a) => a.playState === "running") && (innerWidth >= 900 || document.querySelector(".sidebar").getBoundingClientRect().right <= 0));
    await page.waitForFunction(() => [...document.images].filter((img) => img.getBoundingClientRect().width).every((img) => img.complete && img.naturalWidth > 0));
    const result = await page.evaluate(() => {
      const table = document.querySelector(".business-table");
      const scroll = document.querySelector(".business-table-scroll");
      const sku = table.querySelector("td.table-column-sku_code");
      return {
        allColumns: table.querySelectorAll("th").length === 12,
        allRows: table.querySelectorAll("tbody tr").length === 2,
        skuVisible: getComputedStyle(sku).display !== "none",
        notRanking: !table.closest(".business-table-ranking"),
        stableCellWidth: getComputedStyle(sku).whiteSpace === "nowrap",
        precisionRetained: table.querySelector("td.table-column-received_quantity").textContent.trim() === "1.2345",
        definitionVisible: document.querySelector(".business-result-notes")?.textContent.includes("入库类型为采购（10）"),
        noFalseUnit: !table.querySelector("td.table-column-received_quantity")?.textContent.includes("EA"),
        distinctOwners: table.textContent.includes("TEST-A") && table.textContent.includes("TEST-B"),
        horizontalScroll: scroll.scrollWidth > scroll.clientWidth,
        noPageOverflow: document.documentElement.scrollWidth <= innerWidth,
        questionRetained: document.body.innerText.includes("查询采购入库明细，采用入库类型为采购的口径"),
        noClarification: !document.querySelector(".clarification-card")
      };
    });
    if (Object.values(result).some((value) => !value)) throw new Error(JSON.stringify(result));
  };
  await check();
  await page.screenshot({ path: "output/playwright/receipt-desktop.png" });
  await page.reload();
  await page.waitForSelector(".business-table");
  await check();
  await page.setViewportSize({ width: 375, height: 812 });
  await check();
  await page.screenshot({ path: "output/playwright/receipt-mobile.png" });
  await page.locator(".business-table-scroll").evaluate((el) => { el.scrollLeft = el.scrollWidth; });
  await page.screenshot({ path: "output/playwright/receipt-mobile-end.png" });
  await page.setViewportSize({ width: 812, height: 375 });
  await check();
  if (errors.length) throw new Error(errors.join("\n"));
  return { desktop: true, mobile: true, landscape: true, refresh: true, fullGrain: true, noPageErrors: true };
}
