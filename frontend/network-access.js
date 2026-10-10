(() => {
  const status = document.querySelector("#status");
  const body = document.querySelector("#requests");
  const empty = document.querySelector("#empty");
  const count = document.querySelector("#pending-count");
  let csrfToken = "";

  function setStatus(message, error = false) {
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  async function bootstrap() {
    const response = await fetch("/v1/session", { credentials: "same-origin" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload?.error?.message || "会话初始化失败");
    csrfToken = payload.csrf_token || "";
  }

  function row(request) {
    const tr = document.createElement("tr");
    const values = [request.ip, request.note || "-", request.status, request.first_seen, request.last_seen, String(request.hit_count)];
    for (const value of values) {
      const td = document.createElement("td");
      td.textContent = value || "-";
      tr.append(td);
    }
    const action = document.createElement("td");
    if (request.status === "pending") {
      for (const [label, endpoint, className] of [["批准", "approve", "approve"], ["拒绝", "reject", "reject"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `button ${className}`;
        button.textContent = label;
        button.addEventListener("click", () => update(request.ip, endpoint, button));
        action.append(button, " ");
      }
    } else {
      action.textContent = "-";
    }
    tr.append(action);
    return tr;
  }

  async function load() {
    try {
      const response = await fetch("/v1/network-access/requests", { credentials: "same-origin" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || "申请列表加载失败");
      const requests = Array.isArray(payload.requests) ? payload.requests : [];
      body.replaceChildren(...requests.map(row));
      const pending = requests.filter(item => item.status === "pending").length;
      count.textContent = String(pending);
      empty.hidden = requests.length !== 0;
      setStatus(`共 ${requests.length} 条记录，${pending} 条待处理`);
    } catch (error) {
      setStatus(error.message || "申请列表加载失败", true);
    }
  }

  async function update(ip, endpoint, button) {
    button.disabled = true;
    try {
      const response = await fetch(`/v1/network-access/${endpoint}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ ip }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || "操作失败");
      await load();
    } catch (error) {
      setStatus(error.message || "操作失败", true);
      button.disabled = false;
    }
  }

  document.querySelector("#refresh").addEventListener("click", load);
  bootstrap().then(load).catch(error => setStatus(error.message || "会话初始化失败", true));
})();
