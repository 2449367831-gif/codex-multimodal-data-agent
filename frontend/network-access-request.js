(() => {
  const form = document.querySelector("#request-form");
  const note = document.querySelector("#note");
  const status = document.querySelector("#status");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    status.textContent = "正在提交…";
    try {
      const response = await fetch("/v1/network-access/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: note.value.trim() }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || "申请提交失败");
      status.textContent = payload.status === "approved"
        ? "该 IP 已获批准，请返回 Agent 页面。"
        : "申请已提交，请等待管理员批准。";
      form.querySelector("button").disabled = true;
    } catch (error) {
      status.textContent = error.message || "申请提交失败";
    }
  });
})();
