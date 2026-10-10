async (page) => {
  // Run only after browser-directory-fixture.js has isolated every API request.
  const submit = async (question, predicate) => {
    await page.getByRole("textbox", { name: "输入取数问题" }).fill(question);
    await page.getByRole("button", { name: "提交取数问题" }).click();
    await page.waitForFunction(predicate);
  };
  await page.setViewportSize({ width: 1440, height: 1000 });
  await submit("查询名称包含沈阳的测试仓库，返回编码和名称", () =>
    document.querySelector(".business-table tbody tr") &&
    !document.querySelector(".clarification-card")
  );
  await submit("查询名称包含不存在的测试仓库", () =>
    document.querySelector("#traceOutcome")?.textContent === "查询成功，当前范围无数据"
  );
  await page.reload();
  await page.waitForFunction(() =>
    document.querySelector("#traceOutcome")?.textContent === "查询成功，当前范围无数据" &&
    document.querySelector("#traceStatusText")?.textContent === "查询成功，当前范围无数据"
  );
  await page.setViewportSize({ width: 375, height: 812 });
  const empty = page.locator(".business-table-empty").last();
  await empty.scrollIntoViewIfNeeded();
  const emptyFits = await empty.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return rect.left >= 0 && rect.right <= innerWidth && element.scrollWidth <= element.clientWidth;
  });
  if (!emptyFits) throw new Error("Empty result message is clipped on mobile");
  await page.screenshot({ path: "output/playwright/warehouse-empty-mobile.png" });
  await submit("查询名称包含截断的测试仓库", () =>
    document.body.innerText.includes("不能作为完整结果")
  );
  await page.reload();
  await page.waitForFunction(() => document.body.innerText.includes("不能作为完整结果"));
  const states = await page.evaluate(() => ({
    questionRetained: document.body.innerText.includes("查询名称包含截断的测试仓库"),
    noClarifications: !document.querySelector(".clarification-card"),
    noPrivateDiagnostics: !document.body.innerText.includes("PRIVATE_DIAGNOSTIC"),
    noPageOverflow: document.documentElement.scrollWidth <= innerWidth
  }));
  if (Object.values(states).some((value) => !value)) throw new Error(JSON.stringify(states));
  return { ...states, emptyFits, noDataRestored: true };
}
