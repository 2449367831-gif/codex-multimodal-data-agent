(function (scope) {
  "use strict";

  const QUESTION = "评估本次口径变更";
  const ID = /^kp_[A-Za-z0-9_-]{32}$/;
  const REQUEST = /^[A-Za-z0-9_.:-]{1,128}$/;
  const DECISION = /^[a-f0-9]{64}$/;
  const OPTIONS = Object.freeze({current: "保留当前定义", proposed: "采用本次提案", defer: "暂不确认"});
  const FIELDS = new Set(["业务定义", "计算口径", "统计粒度", "计量单位", "指标名称", "口径说明", "口径版本", "修订日期"]);
  const NOTICE = "仅评估本次提案，未查询数据、未发布口径；历史结果不能直接与变更后的口径比较。";
  const STATES = new Set(["proposal_assessed_not_executed", "proposal_selected_not_executed", "proposal_deferred_not_executed"]);
  const UNSAFE = /[`<>\x00-\x1f]|\b(?:select|insert|update|delete|drop|truncate|alter|create|merge|with|from|join|where|union|having|group\s+by|order\s+by|information_schema|sql|bindings?|credentials?|password|secret|access[_ -]?token|api[_ -]?key|webhook|bearer)\b|\bsk-[A-Za-z0-9_-]{8,}|\b[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*\b|\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b|[A-Za-z_]\w*\s*\(/i;
  const ERRORS = Object.freeze({
    proposal_unavailable: "此评估已不可用，请重新提交材料；未查询数据。",
    proposal_expired: "此评估已过期或取消，请重新提交材料；未查询数据。",
    proposal_baseline_changed: "正式口径已变化，请基于最新口径重新评估；未提交选择。",
    proposal_conflict: "评估版本已变化，请恢复最新状态后再选择。",
    proposal_invalid: "材料或选择不完整，未提交任何变更。",
    proposal_description_required: "材料包含尚不能展示的内部定义，需要补充可核对的业务口径说明。",
    proposal_capacity: "评估数量已达上限，请取消不再需要的评估后重试。",
    invalid_response: "评估响应不完整，未展示或提交任何选择。"
  });
  const TERMINAL_ERRORS = new Set(["proposal_unavailable", "proposal_expired", "proposal_baseline_changed", "invalid_response", "proposal_description_required", "proposal_invalid"]);
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = (value) => typeof value === "string" && value.length > 0 && value.length <= 240 && !UNSAFE.test(value) &&
    !/(?:查询语句|数据库凭据|审计内容|执行日志|原始数据行|原始结果行|字段列表|表结构)/.test(value);
  const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));

  function normalize(raw) {
    if (!object(raw) || raw.result_state !== "definition_review" || raw.process?.state !== "not_queried" ||
        !Array.isArray(raw.process.steps) || raw.process.steps.length || !ID.test(raw.proposal_id) ||
        !Number.isSafeInteger(raw.revision) || raw.revision < 0 || !Number.isSafeInteger(raw.expires_at) || raw.expires_at <= 0) return null;
    const a = raw.assessment;
    if (!object(a) || !STATES.has(a.state) || a.title !== "口径变更评估" || a.notice !== NOTICE || a.publication_authorized !== false ||
        typeof a.requires_confirmation !== "boolean" || typeof a.assessment_complete !== "boolean" ||
        !["需要修正依赖关系", "已检查声明的依赖关系"].includes(a.dependency_state) ||
        !Array.isArray(a.questions) || a.questions.length > 128 || !Array.isArray(a.comparisons) ||
        a.comparisons.length !== a.questions.length || !object(a.choices)) return null;
    const seen = new Set();
    const comparisons = [];
    for (let index = 0; index < a.questions.length; index++) {
      const q = a.questions[index], row = a.comparisons[index];
      if (!object(q) || !object(row) || !DECISION.test(q.id) || seen.has(q.id) || row.decision_id !== q.id ||
          !FIELDS.has(row.field) || !text(row.metric) || !text(row.before) || !text(row.after) ||
          q.prompt !== `${row.metric}：${row.field}` || q.selection !== "single" || !Array.isArray(q.options) || q.options.length !== 3 ||
          q.options.some((option, i) => option?.id !== Object.keys(OPTIONS)[i] || option.label !== Object.values(OPTIONS)[i])) return null;
      seen.add(q.id);
      comparisons.push({decision_id: q.id, metric: row.metric, field: row.field, before: row.before, after: row.after});
    }
    if (Object.entries(a.choices).some(([id, choice]) => !seen.has(id) || !Object.hasOwn(OPTIONS, choice))) return null;
    const selected = Object.keys(a.choices).length, deferred = Object.values(a.choices).includes("defer");
    if (a.state === "proposal_assessed_not_executed" && (raw.revision !== 0 || selected || a.requires_confirmation !== (seen.size > 0))) return null;
    if (a.state !== "proposal_assessed_not_executed" && (raw.revision < 1 || selected !== seen.size ||
        a.requires_confirmation !== deferred || (a.state === "proposal_deferred_not_executed") !== deferred)) return null;
    return {proposalId: raw.proposal_id, revision: raw.revision, expiresAt: raw.expires_at,
      assessment: {state: a.state, comparisons, choices: {...a.choices}, requiresConfirmation: a.requires_confirmation,
        complete: a.assessment_complete, dependency: a.dependency_state}};
  }

  // Persist references and enum choices only. Definitions and submitted material
  // stay out of local history; resume revalidates the server-owned assessment.
  function serialize(event) {
    if (!REQUEST.test(event.requestId || "")) return null;
    return {kind: "proposal", requestId: event.requestId, proposalId: ID.test(event.proposalId || "") ? event.proposalId : "",
      revision: Number.isSafeInteger(event.revision) ? event.revision : 0,
      draftChoices: Object.fromEntries(Object.entries(event.draftChoices || {}).filter(([id, choice]) => DECISION.test(id) && Object.hasOwn(OPTIONS, choice))),
      cancelled: event.cancelled === true, createdAt: Number.isFinite(event.createdAt) ? event.createdAt : Date.now()};
  }

  function restore(raw) {
    if (!object(raw) || !REQUEST.test(raw.requestId || "") || (raw.proposalId && !ID.test(raw.proposalId))) return null;
    const saved = serialize(raw);
    return {...saved, verified: false, busy: false, assessment: null, error: saved.cancelled ? "" : saved.proposalId ? "" : "评估请求在刷新时中断，原始材料未保留，请重新提交。"};
  }

  function create(host) {
    let readNonce = 0, reading = false;
    const state = host.state;
    const sessionsClosing = new WeakSet();
    const alive = (session, conversation) => state.sessionReady && state.backendConversationId === conversation &&
      state.sessions.includes(session) && !sessionsClosing.has(session);
    const binding = (session, event) => ({conversation_id: state.backendConversationId, display_session_id: session.id, request_id: event.requestId});
    const post = (url, body) => host.fetchJson(url, {method: "POST", headers: {"Content-Type":"application/json", "X-CSRF-Token": state.csrfToken}, body: JSON.stringify(body)}, 15000);
    const errorText = (error) => ERRORS[error?.code] || "连接中断，尚未确认评估状态。请恢复状态后继续；不会自动重复提交。";
    const blocked = (session) => reading || sessionsClosing.has(session) || session.events.some((event) => event.kind === "proposal" && event.busy);
    const refresh = (session) => host.refresh(session);

    function accept(event, raw, keepDraft = false) {
      const result = normalize(raw);
      if (!result || (event.proposalId && event.proposalId !== result.proposalId) || result.revision < event.revision) throw {code: "invalid_response"};
      const draft = keepDraft && event.revision === result.revision ? event.draftChoices : null;
      Object.assign(event, result, {verified: true, error: "", terminal: false,
        draftChoices: {...result.assessment.choices, ...Object.fromEntries(Object.entries(draft || {}).filter(([id, choice]) =>
          result.assessment.comparisons.some((row) => row.decision_id === id) && Object.hasOwn(OPTIONS, choice)))}});
    }

    async function act(session, event, action) {
      if (event.busy || !state.sessionReady || !state.sessions.includes(session) || sessionsClosing.has(session)) return;
      if (action === "select" && (!event.verified || !event.assessment?.requiresConfirmation)) return;
      const body = binding(session, event), conversation = body.conversation_id;
      if (action === "select") {
        const ids = event.assessment.comparisons.map((row) => row.decision_id);
        if (ids.some((id) => !Object.hasOwn(OPTIONS, event.draftChoices[id]))) {
          event.error = "请完成全部字段的选择后一次提交。"; refresh(session);
          const missing = ids.find((id) => !Object.hasOwn(OPTIONS, event.draftChoices[id]));
          host.container.querySelector(`[data-proposal-index="${session.events.indexOf(event)}"] [data-decision="${missing}"]`)?.focus();
          return;
        }
        body.revision = event.revision;
        body.choices = Object.fromEntries(ids.map((id) => [id, event.draftChoices[id]]));
      }
      event.busy = true; event.error = ""; refresh(session);
      try {
        const result = await post(`/v1/knowledge-proposals/${event.proposalId}/${action}`, body);
        if (!alive(session, conversation)) return;
        if (action === "cancel") {
          if (result.state !== "cancelled") throw {code: "invalid_response"};
          event.cancelled = true; event.assessment = null; event.verified = false; event.draftChoices = {};
        } else accept(event, result, action === "resume");
      } catch (error) {
        if (!alive(session, conversation)) return;
        event.verified = false; event.error = errorText(error); event.terminal = TERMINAL_ERRORS.has(error?.code);
      } finally {
        event.busy = false;
        if (alive(session, conversation)) {
          refresh(session);
          if (action !== "resume" && host.activeSession() === session) {
            host.container.querySelector(`[data-proposal-index="${session.events.indexOf(event)}"] [role="status"]`)?.focus({preventScroll:true});
          }
        }
      }
    }

    async function resumeSession(session) {
      for (const event of session.events) {
        if (host.activeSession() !== session || !state.sessionReady) return;
        if (event.kind === "proposal" && event.proposalId && !event.cancelled && !event.verified && !event.terminal && !event.busy) await act(session, event, "resume");
      }
    }

    async function closeSession(session) {
      if (!session.events.some((event) => event.kind === "proposal" || event.kind === "interpretation")) return true;
      if (!state.sessionReady || sessionsClosing.has(session)) return false;
      const conversation = state.backendConversationId;
      sessionsClosing.add(session); refresh(session);
      try {
        const result = await post("/v1/knowledge-proposal-sessions/close", {conversation_id: conversation, display_session_id: session.id});
        if (result.state !== "closed") throw {code: "invalid_response"};
        return state.backendConversationId === conversation && state.sessions.includes(session);
      } catch {
        host.toast("会话暂未删除：未能确认口径评估已关闭，请重试。"); return false;
      } finally { sessionsClosing.delete(session); refresh(session); }
    }

    function render(event, index) {
      const a = event.assessment;
      const closing = sessionsClosing.has(host.activeSession());
      const editable = event.verified && a?.requiresConfirmation && !event.busy && !event.cancelled && !closing;
      const status = event.cancelled ? "评估已取消" : closing ? "正在关闭评估" : event.busy ? "正在处理口径评估" :
        event.terminal || (!event.proposalId && event.error) ? "评估未完成" : !event.verified ? "等待恢复评估状态" :
        a.requiresConfirmation ? "口径待选择" : a.comparisons.length ? "选择已记录，尚未发布" : "未发现字段变更";
      const rows = a?.comparisons.map((row) => `<tr><th scope="row">${escape(row.metric)}<small>${escape(row.field)}</small></th><td>${escape(row.before)}</td><td>${escape(row.after)}</td></tr>`).join("") || "";
      const choices = a?.comparisons.map((row) => `<fieldset class="proposal-field" ${editable ? "" : "disabled"}><legend>${escape(row.metric)} · ${escape(row.field)}</legend><div class="proposal-options">${Object.entries(OPTIONS).map(([id, label]) =>
        `<label class="choice-option"><input type="radio" name="proposal-${index}-${row.decision_id}" data-decision="${row.decision_id}" value="${id}" ${event.draftChoices[row.decision_id] === id ? "checked" : ""} /><span>${label}</span></label>`).join("")}</div></fieldset>`).join("") || "";
      return `<article class="message assistant proposal-message"><div class="message-avatar" aria-hidden="true">${host.avatar()}</div><div class="message-body">` +
        `<div class="message-meta">皇家小虎取数运行时 · 未查询数据</div><section class="proposal-review" data-proposal-index="${index}" aria-label="口径变更评估" aria-busy="${event.busy ? "true" : "false"}">` +
        `<header class="proposal-header"><h3>口径变更评估</h3><span role="status" tabindex="-1">${status}</span></header><p class="proposal-notice">${NOTICE}</p>` +
        (rows ? `<div class="proposal-table-scroll" tabindex="0" role="region" aria-label="字段差异"><table class="proposal-table"><thead><tr><th scope="col">指标与字段</th><th scope="col">当前定义</th><th scope="col">提案定义</th></tr></thead><tbody>${rows}</tbody></table></div><p class="proposal-dependency">${escape(a.dependency)}</p>` : "") +
        `<form data-proposal-form="${index}">${choices}<p class="proposal-error" role="alert" ${event.error ? "" : "hidden"}>${escape(event.error || "")}</p><div class="proposal-actions">` +
        (editable ? `<button type="submit" class="primary-button"><svg class="icon" aria-hidden="true"><use href="#icon-check" /></svg>一次提交全部选择</button>` : "") +
        (!event.cancelled && event.proposalId && !event.verified && !event.terminal ? `<button type="button" class="secondary-button" data-proposal-action="resume" ${event.busy || closing ? "disabled" : ""}>恢复评估状态</button>` : "") +
        (!event.cancelled && event.proposalId ? `<button type="button" class="secondary-button" data-proposal-action="cancel" ${event.busy || closing ? "disabled" : ""}>取消评估</button>` : "") +
        `</div></form></section></div></article>`;
    }

    function bind(container, session) {
      container.querySelectorAll("[data-proposal-index]").forEach((panel) => {
        const event = session.events[Number(panel.dataset.proposalIndex)];
        panel.querySelector("form").addEventListener("submit", (input) => { input.preventDefault(); void act(session, event, "select"); });
        panel.querySelectorAll("[data-proposal-action]").forEach((button) => button.addEventListener("click", () => void act(session, event, button.dataset.proposalAction)));
        panel.addEventListener("change", (input) => {
          if (!event.verified || event.busy || !event.assessment?.requiresConfirmation) return;
          const id = input.target.dataset.decision, choice = input.target.value;
          if (!event.assessment.comparisons.some((row) => row.decision_id === id) || !Object.hasOwn(OPTIONS, choice)) return;
          event.draftChoices[id] = choice; event.error = "";
          panel.querySelector(".proposal-error").hidden = true;
          host.persist();
        });
      });
    }

    function clearInput() {
      readNonce++; reading = false;
      host.file.value = ""; host.inputStatus.hidden = true;
    }

    host.upload.addEventListener("click", () => {
      if (host.question.value.trim()) { host.toast("请先提交或清空当前问题，再提交口径评估材料"); return; }
      host.file.click();
    });
    host.file.addEventListener("change", async () => {
      const file = host.file.files?.[0]; host.file.value = "";
      const session = host.activeSession(), conversation = state.backendConversationId;
      if (!file || !state.sessionReady || state.loading || blocked(session)) return;
      const nonce = ++readNonce; reading = true;
      host.inputStatus.hidden = false; host.inputStatus.textContent = "正在读取口径评估材料"; host.updateComposer();
      let event;
      try {
        if (!file.name.toLowerCase().endsWith(".json") || file.size > 200000) throw {code: "proposal_invalid"};
        const raw = await file.text();
        const parsed = JSON.parse(raw);
        if (!object(parsed) || Object.keys(parsed).sort().join(",") !== "baseline_sha256,changes") throw {code: "proposal_invalid"};
        if (nonce !== readNonce || host.activeSession() !== session || !alive(session, conversation)) return;
        if (host.question.value.trim()) throw {code: "proposal_invalid"};
        event = {kind: "proposal", requestId: host.randomId("review"), proposalId: "", revision: 0, draftChoices: {}, createdAt: Date.now(), busy: true};
        session.events.push({kind:"question", question:QUESTION, createdAt:Date.now()}, event);
        reading = false; host.inputStatus.hidden = true; refresh(session); host.resetInspector(); host.updateComposer();
        // Send the exact JSON string to preserve decimals and duplicate-key checks.
        const result = await post("/v1/knowledge-proposals", {...binding(session, event), proposal: raw});
        if (!alive(session, conversation)) return;
        accept(event, result);
      } catch (error) {
        if (!alive(session, conversation) || (!event && nonce !== readNonce)) return;
        if (event) {
          event.error = ERRORS[error?.code] || "评估请求中断，原始材料未保留。请先删除本界面会话以关闭未确认的评估，再重新提交材料。";
          event.terminal = true;
        }
        else { host.inputStatus.textContent = "材料未提交：请选择不超过 200 KB 的完整口径评估材料文件。"; host.inputStatus.hidden = false; }
      } finally {
        if (event) { event.busy = false; if (alive(session, conversation)) refresh(session); }
        if (nonce === readNonce) { reading = false; host.updateComposer(); }
      }
    });

    return {render, bind, serialize, restore, resumeSession, closeSession, clearInput, blocked, isClosing: session => sessionsClosing.has(session)};
  }

  scope.RoyalProposalReview = Object.freeze({create, normalize, serialize, restore, safeText: text});
})(typeof window === "undefined" ? globalThis : window);
