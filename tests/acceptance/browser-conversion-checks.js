async (page) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const question = "将 MySQL 查询转换为 Doris";
  await page.setViewportSize({width: 1440, height: 1000});
  await page.getByRole("textbox", {name: "输入取数问题"}).fill(question);
  await page.getByRole("button", {name: "提交取数问题"}).click();
  await page.waitForFunction(() => document.querySelector(".guidance-message")?.textContent.includes("待补充转换材料"));
  if (await page.locator(".clarification-card").count()) throw new Error("Unrelated clarification");
  await page.locator("#conversionFile").setInputFiles("tests/fixtures/conversion/PRIVATE_FILENAME.json");
  await page.waitForFunction(() => document.body.innerText.includes("转换检查完成，未查询数据"));
  const check = async () => {
    await page.waitForFunction(() => [...document.images].filter((img) => img.getBoundingClientRect().width).every((img) => img.complete && img.naturalWidth));
    await page.waitForFunction(() => !document.getAnimations().some((animation) => animation.playState === "running"));
    const flags = await page.evaluate(() => ({
      noPrivateText: !/PRIVATE_INPUT|PRIVATE_FILENAME|SELECT|不应出现的伪造业务结果/.test(document.body.innerText),
      noPrivateStorage: !/PRIVATE_INPUT|PRIVATE_FILENAME|SELECT|sql_conversion"\s*:"\{/.test(JSON.stringify(localStorage)),
      noFakeQuery: document.querySelector("#traceProcess").children.length === 0 && !document.querySelector(".guidance-message .execution-card"),
      coherentState: document.querySelector("#traceStatusText").textContent === document.querySelector("#traceOutcome").textContent,
      noOverflow: document.documentElement.scrollWidth <= innerWidth,
      attachmentTarget: document.querySelector("#conversionUpload").getBoundingClientRect().height >= 44
    }));
    if (Object.values(flags).some((value) => !value)) throw new Error(JSON.stringify(flags));
  };
  const flags = await page.evaluate(() => window.conversionFixtureFlags);
  if (!flags.privateField || !flags.originalDecimal || !flags.questionSafe) throw new Error("Private input transport changed");
  await check();
  await page.reload();
  await page.waitForFunction(() => document.body.innerText.includes("转换检查完成，未查询数据"));
  await check();
  await page.screenshot({path: "output/playwright/conversion-desktop.png"});
  await page.emulateMedia({reducedMotion: "reduce"});
  for (const size of [{width:375,height:812},{width:812,height:375}]) {
    await page.setViewportSize(size);
    await check();
    await page.screenshot({path: `output/playwright/conversion-${size.width}.png`});
  }
  await page.getByRole("button", {name:"切换外观"}).click();
  await check();
  await page.screenshot({path:"output/playwright/conversion-dark.png"});
  await page.locator("#conversionFile").setInputFiles("tests/fixtures/conversion/invalid.json");
  await page.waitForFunction(() => !document.querySelector("#conversionInputStatus").hidden);
  if ((await page.locator("body").innerText()).includes("invalid-private-input")) throw new Error("File parse error leaked");
  await page.locator("#conversionFile").setInputFiles("tests/fixtures/conversion/rejected.json");
  await page.waitForFunction(() => document.body.innerText.includes("转换未通过，未查询数据"));
  await check();
  await page.reload();
  await page.waitForFunction(() => document.body.innerText.includes("转换未通过，未查询数据"));
  await check();
  await page.getByRole("textbox", {name: "输入取数问题"}).fill("不安全的转换结果测试");
  await page.getByRole("button", {name: "提交取数问题"}).click();
  await page.waitForFunction(() => document.querySelector(".execution-message"));
  if ((await page.locator("body").innerText()).includes("PRIVATE_RESPONSE")) throw new Error("Unsafe response leaked");
  if (errors.length) throw new Error(errors.join("\n"));
  return {intake:true,decimalPreserved:true,nonQueryStates:true,privacy:true,refresh:true,desktop:true,mobile:true,landscape:true,dark:true};
}
