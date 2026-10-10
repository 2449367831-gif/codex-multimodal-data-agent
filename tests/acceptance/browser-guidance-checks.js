async (page) => {
  // Only run after browser-guidance-fixture.js has isolated all API requests.
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const question = "请说明后端拒绝、执行失败和无数据有什么区别";
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("textbox", { name: "输入取数问题" }).fill(question);
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.waitForFunction(() => document.querySelector(".guidance-message"));
  const check = async () => {
    await page.waitForFunction(() => [...document.images].filter((img) => img.getBoundingClientRect().width).every((img) => img.complete && img.naturalWidth > 0));
    await page.waitForFunction(() =>
      !document.getAnimations().some((animation) => animation.playState === "running") &&
      (innerWidth >= 900 || document.querySelector(".sidebar").getBoundingClientRect().right <= 0)
    );
    const result = await page.evaluate(() => ({
      allStatuses: ["后端拒绝", "执行失败", "成功无数据", "结果截断"].every((s) => document.querySelector(".guidance-message")?.textContent.includes(s)),
      notQueried: document.querySelector("#traceOutcome")?.textContent === "说明已完成，未查询数据",
      noFakeExecution: !document.querySelector(".guidance-message .execution-card"),
      noFalseData: !document.body.innerText.includes("不应展示的伪造数据"),
      noSelections: !document.querySelector(".clarification-card"),
      noOverflow: document.documentElement.scrollWidth <= innerWidth,
      noQueryStages: document.querySelector("#traceProcess")?.children.length === 0,
      imagesLoaded: [...document.querySelectorAll("img")].filter((img) => img.getBoundingClientRect().width).every((img) => img.complete && img.naturalWidth > 0)
    }));
    if (Object.values(result).some((value) => !value)) throw new Error(JSON.stringify(result));
    return result;
  };
  await check();
  await page.screenshot({ path: "output/playwright/guidance-desktop.png" });
  await page.reload();
  await page.waitForFunction(() => document.querySelector(".guidance-message"));
  await check();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 375, height: 812 });
  await check();
  await page.screenshot({ path: "output/playwright/guidance-mobile.png" });
  await page.setViewportSize({ width: 812, height: 375 });
  await check();
  await page.getByRole("textbox", { name: "输入取数问题" }).fill("不安全的状态说明测试");
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.waitForFunction(() => document.querySelector(".execution-message .execution-status-message"));
  if ((await page.locator("body").innerText()).includes("PRIVATE_DIAGNOSTIC")) throw new Error("Unsafe guidance leaked");
  if (errors.length) throw new Error(errors.join("\n"));
  return { desktop: true, mobile: true, landscape: true, refresh: true, unsafeRejected: true, noPageErrors: true };
}
