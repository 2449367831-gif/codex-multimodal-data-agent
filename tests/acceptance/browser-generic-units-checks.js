async page => {
  await page.getByRole("textbox", {name: "输入取数问题"}).fill("查询合成商品的日均数量、金额和变化率");
  await page.getByRole("button", {name: "提交取数问题", exact: true}).click();
  await page.locator(".business-table").waitFor();
  const reports = [];
  for (const viewport of [{width: 1440, height: 1000}, {width: 390, height: 844}]) {
    await page.setViewportSize(viewport);
    for (let reload = 0; reload < 2; reload++) {
      if (reload) {
        await page.reload();
        await page.locator(".business-table").waitFor();
      }
      const checks = await page.evaluate(() => {
        const table = document.querySelector(".business-table");
        const cells = [...table.querySelectorAll("tbody tr")].map(row => [...row.querySelectorAll("td")]);
        const text = cell => cell.textContent.replaceAll(",", "").trim();
        return {
          headerUnits: [...table.querySelectorAll("th small")].map(n => n.textContent).join("|") === "EA/日|万元|%",
          exactIdentifier: text(cells[0][0]) === "001234567890123456789",
          exactPrecision: text(cells[0][1]) === "-12345678901234567890.00100EA/日",
          moneyUnit: text(cells[0][2]) === "12.3400万元",
          signedPercentage: text(cells[0][3]) === "-0.0100%",
          zeroNotMissing: text(cells[1][2]) === "0.0000万元",
          missingNotZero: !/[0-9]/.test(text(cells[1][1]) + text(cells[1][3])),
          noPageOverflow: document.documentElement.scrollWidth <= innerWidth,
        };
      });
      if (Object.values(checks).some(value => !value)) throw new Error(JSON.stringify(checks));
      reports.push({viewport, reload: !!reload, checks});
    }
    await page.screenshot({path: `output/playwright/generic-units-${viewport.width}.png`});
  }
  return reports;
}
