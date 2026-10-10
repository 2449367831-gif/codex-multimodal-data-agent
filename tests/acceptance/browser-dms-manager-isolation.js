async (page) => {
  const control = (mode) => page.evaluate((value) => fetch("/v1/fixture", {method: "POST", body: JSON.stringify({mode: value})}).then((r) => r.json()), mode);
  const submit = async () => {
    await page.getByRole("textbox", {name: "输入取数问题"}).fill("查询DMS销售订单，按日期、省区、城市经理、客户编码、客户名称统计订单数");
    await page.getByRole("button", {name: "提交取数问题"}).click();
    await page.waitForSelector(".business-table", {timeout: 6000});
  };
  await page.setViewportSize({width: 1440, height: 1000});
  await submit();
  const original = await page.locator('[data-session-id][aria-current="page"]').getAttribute("data-session-id");
  await control("pending");
  await page.getByRole("button", {name: "下一页", exact: true}).click();
  await page.waitForFunction(async () => (await fetch("/v1/fixture").then((r) => r.json())).gets === 1);
  await page.getByRole("button", {name: "新建界面会话", exact: true}).click();
  await control("success");
  await page.waitForFunction(() => Object.values(localStorage).some((raw) => raw.includes('"start_row":101')));
  if (await page.locator(".business-table").count()) throw new Error("Background page leaked into new session");
  await page.locator(`[data-session-id="${original}"]`).click();
  await page.waitForFunction(() => document.querySelectorAll(".business-table tbody tr").length === 5);

  await control("pending");
  await page.getByRole("button", {name: "上一页", exact: true}).click();
  await page.waitForFunction(async () => (await fetch("/v1/fixture").then((r) => r.json())).gets === 2);
  await page.locator(`[data-delete-session-id="${original}"]`).click();
  await control("success");
  await page.reload();
  await page.waitForSelector("[data-session-id]");
  if (await page.locator(`[data-session-id="${original}"]`).count() || await page.locator(".business-table").count()) throw new Error("Late page restored deleted session");

  await control("empty");
  await submit();
  await page.waitForSelector(".business-table-empty");
  if (await page.locator(".business-table th").count() !== 9 || await page.locator(".business-table tbody tr").count() || await page.locator(".result-pagination").count()) throw new Error("Empty result lost schema or fabricated rows");
  if (!(await page.locator(".execution-message").innerText()).includes("查询成功")) throw new Error("Empty result reported as failure");
  await page.reload();
  await page.waitForSelector(".business-table-empty");
  if (await page.locator(".business-table tbody tr").count() || await page.locator(".result-pagination").count()) throw new Error("Refresh fabricated empty rows");
  return {backgroundOwnership:true,deletedSessionStaysDeleted:true,emptySchema:true,emptyRefresh:true};
}
