async (page) => {
  // Run after the conversion fixture. All APIs and materials are synthetic.
  await page.setViewportSize({width: 1440, height: 1000});
  const input = page.getByRole("textbox", {name: "输入取数问题"});
  let posts = 0;
  const count = (request) => {
    if (request.method() === "POST" && request.url().endsWith("/v1/tasks")) posts += 1;
  };
  page.on("request", count);
  const deferRead = async () => page.evaluate(() => {
    const original = File.prototype.text;
    File.prototype.text = function () {
      return new Promise((resolve, reject) => {
        window.finishConversionRead = async () => {
          File.prototype.text = original;
          try { resolve(await original.call(this)); } catch (error) { reject(error); }
        };
      });
    };
  });
  await deferRead();
  await page.locator("#conversionFile").setInputFiles("tests/fixtures/conversion/PRIVATE_FILENAME.json");
  await page.waitForFunction(() => typeof window.finishConversionRead === "function");
  await input.fill("尚未提交的问题");
  await input.press("Enter");
  if (posts !== 0 || await input.inputValue() !== "尚未提交的问题") throw new Error("Enter raced material read");
  await page.evaluate(() => window.finishConversionRead());
  await page.waitForFunction(() => document.querySelector("#conversionInputStatus").hidden);
  if (posts !== 0) throw new Error("Materials replaced typed question");
  await input.fill("");

  await deferRead();
  await page.locator("#conversionFile").setInputFiles("tests/fixtures/conversion/PRIVATE_FILENAME.json");
  await page.getByRole("button", {name: "新建界面会话"}).click();
  await page.evaluate(() => window.finishConversionRead());
  if (posts !== 0 || await page.locator(".guidance-message").count()) throw new Error("Late read crossed sessions");

  let hold = true;
  const holdTask = async (route) => {
    if (!hold) return route.fallback();
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({
      task_id: route.request().url().split("/").pop(), state: "running", stage: "routing"
    })});
  };
  await page.route("**/v1/tasks/*", holdTask);
  await page.locator("#conversionFile").setInputFiles("tests/fixtures/conversion/PRIVATE_FILENAME.json");
  await page.waitForFunction(() => Object.values(localStorage).some((value) => {
    try { return JSON.parse(value).sessions?.some((s) => s.events.some((e) => e.kind === "running" && e.taskId)); }
    catch { return false; }
  }));
  await page.reload();
  await page.waitForFunction(() => document.body.innerText.includes("正在检查转换材料"));
  if (posts !== 1) throw new Error("Refresh reposted private materials");
  hold = false;
  await page.waitForFunction(() => document.body.innerText.includes("转换检查完成，未查询数据"));
  if (posts !== 1) throw new Error("Task recovery repeated conversion");
  await page.unroute("**/v1/tasks/*", holdTask);

  await page.evaluate(() => {
    for (const key of Object.keys(localStorage)) {
      let saved;
      try { saved = JSON.parse(localStorage.getItem(key)); } catch { continue; }
      if (!saved?.sessions) continue;
      const session = saved.sessions.find((s) => s.id === saved.activeId);
      const event = session.events.findLast((e) => e.kind === "execution");
      session.events = [{kind: "question", question: event.question, createdAt: Date.now()}, {
        kind: "running", question: event.question, requestKey: event.requestKey,
        conversionSubmitted: true, taskId: "", createdAt: Date.now()
      }];
      localStorage.setItem(key, JSON.stringify(saved));
    }
  });
  await page.reload();
  await page.waitForFunction(() => document.body.innerText.includes("原始转换材料未保留，请重新提交材料"));
  if (posts !== 1) throw new Error("Missing materials were silently resubmitted");
  const privateData = await page.evaluate(() => /PRIVATE_INPUT|PRIVATE_FILENAME|SELECT/.test(document.body.innerText + JSON.stringify(localStorage)));
  if (privateData) throw new Error("Recovery leaked private material");
  page.off("request", count);
  return {enterRace:true,sessionIsolation:true,pendingRefresh:true,noMaterialReplay:true,privacy:true};
}
