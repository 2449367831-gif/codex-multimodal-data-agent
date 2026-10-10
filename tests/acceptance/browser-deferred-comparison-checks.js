async (page) => {
  await page.route("**/v1/tasks", async route => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({
      task_id: "task_00000000000000000000000000000004", state: "failed", http_status: 409,
      result: { error: { code: "business_definition_unpublished", result_state: "blocked", message: "private diagnostic must not appear" } }
    })
  }));
  const question = "查询昨天实际出库数量和分配数量的差异";
  await page.getByRole("textbox", { name: "输入取数问题" }).fill(question);
  await page.getByRole("button", { name: "提交取数问题" }).click();
  await page.waitForSelector(".message-error");
  await page.reload();
  await page.waitForSelector(".message-error");
  await page.emulateMedia({ reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 375, height: 812 }, { width: 812, height: 375 }]) {
    await page.setViewportSize(viewport);
    const checks = await page.evaluate(asked => ({
      correctReason: document.querySelector(".message-error").textContent.includes("单位关系尚未确认"),
      noQueryLabel: document.querySelector("[role=alert] .message-meta").textContent.includes("未执行查询"),
      inspectorConsistent: document.querySelector("#traceStatusText").textContent.includes("未执行查询"),
      questionRetained: document.querySelector(".error-question").textContent.includes(asked),
      noPointlessRetry: !document.querySelector(".retry-button"),
      noChoices: !document.querySelector(".clarification-card"),
      noData: !document.querySelector(".business-table"),
      noPrivateDiagnostics: !document.body.textContent.includes("private diagnostic"),
      noOverflow: document.documentElement.scrollWidth <= innerWidth,
      announced: document.querySelector(".message-error").closest("[role=alert]") !== null
    }), question);
    if (Object.values(checks).some(value => !value)) throw new Error(JSON.stringify(checks));
    await page.screenshot({ path: `output/playwright/deferred-comparison-${viewport.width}.png` });
  }
  return { clearReason: true, questionAndRefresh: true, noPointlessRetry: true, desktop: true, mobile: true, landscape: true };
}
