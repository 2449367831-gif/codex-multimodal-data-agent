(function (scope) {
  "use strict";
  const ID = /^ki_[A-Za-z0-9_-]{32}$/;
  const PROPOSAL = /^kp_[A-Za-z0-9_-]{32}$/;
  const REQUEST = /^[A-Za-z0-9_.:-]{1,128}$/;
  const QID = /^q(?:0|[1-9][0-9]{0,2})$/;
  const OPTION = /^(?:o(?:0|[1-9][0-9]{0,2})|defer)$/;
  const STATES = new Set(["running", "needs_confirmation", "deferred", "review_ready", "no_change", "failed", "cancelled"]);
  const ERRORS = Object.freeze({
    interpretation_unavailable: "本次口径请求已不可用，未重新发起解析。",
    interpretation_expired: "本次口径请求已过期，未重新发起解析。",
    interpretation_baseline_changed: "正式口径已更新，请重新提交本次口径需求。",
    interpretation_conflict: "请求版本已变化，请恢复最新状态后再选择。",
    interpretation_invalid: "口径描述或选择不完整，未修改任何口径。",
    interpretation_capacity: "待处理口径较多，请稍后重新提交。",
    interpretation_description_required: "口径选项尚不能直接展示，未提交确认。",
    invalid_response: "口径响应不完整，未展示或提交任何选择。"
  });
  const TERMINAL = new Set(Object.keys(ERRORS).filter(key => key !== "interpretation_conflict"));
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const safeText = scope.RoyalProposalReview.safeText;
  const escape = value => String(value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));

  function normalize(raw) {
    if (!object(raw) || !ID.test(raw.interpretation_id) || !STATES.has(raw.state) ||
        raw.result_state !== "definition_interpretation" || raw.process?.state !== "not_queried" ||
        !Array.isArray(raw.process.steps) || raw.process.steps.length || !Number.isSafeInteger(raw.revision) || raw.revision < 0 ||
        !Number.isSafeInteger(raw.expires_at) || raw.expires_at <= 0 || !Array.isArray(raw.questions) || raw.questions.length > 128 ||
        !object(raw.choices) || (raw.error !== null && !safeText(raw.error))) return null;
    const seen = new Set(), questions = [];
    for (const q of raw.questions) {
      if (!object(q) || !QID.test(q.id) || seen.has(q.id) || !safeText(q.prompt) || q.selection !== "single" ||
          !Array.isArray(q.options) || q.options.length < 2 || q.options.length > 129) return null;
      const options = [], ids = new Set();
      for (const option of q.options) {
        if (!object(option) || !OPTION.test(option.id) || ids.has(option.id) || !safeText(option.label) ||
            (option.id === "defer") !== (option.label === "暂不确认")) return null;
        ids.add(option.id); options.push({id:option.id, label:option.label});
      }
      if (!ids.has("defer")) return null;
      seen.add(q.id); questions.push({id:q.id, prompt:q.prompt, options});
    }
    if (Object.entries(raw.choices).some(([id, value]) => !questions.some(q => q.id === id && q.options.some(o => o.id === value)))) return null;
    if (["needs_confirmation", "deferred"].includes(raw.state) && !questions.length) return null;
    if (raw.state === "deferred" && (Object.keys(raw.choices).length !== questions.length || !Object.values(raw.choices).includes("defer"))) return null;
    if ((raw.state === "review_ready") !== Boolean(raw.proposal) || (raw.proposal && (!object(raw.proposal) || !PROPOSAL.test(raw.proposal.proposal_id))) ||
        (["review_ready", "no_change", "cancelled"].includes(raw.state) && (questions.length || Object.keys(raw.choices).length)) ||
        (raw.state === "failed") !== Boolean(raw.error)) return null;
    return {interpretationId:raw.interpretation_id, revision:raw.revision, expiresAt:raw.expires_at,
      status:raw.state, questions, choices:{...raw.choices}, proposalId:raw.proposal?.proposal_id || "", error:raw.error || ""};
  }

  function serialize(event) {
    if (!REQUEST.test(event.requestId || "")) return null;
    return {kind:"interpretation", requestId:event.requestId,
      interpretationId:ID.test(event.interpretationId || "") ? event.interpretationId : "",
      revision:Number.isSafeInteger(event.revision) && event.revision >= 0 ? event.revision : 0,
      draftChoices:Object.fromEntries(Object.entries(event.draftChoices || {}).filter(([id, value]) => QID.test(id) && OPTION.test(value))),
      cancelled:event.cancelled === true, createdAt:Number.isFinite(event.createdAt) ? event.createdAt : Date.now()};
  }
  function restore(raw) {
    if (!object(raw) || !REQUEST.test(raw.requestId || "") || (raw.interpretationId && !ID.test(raw.interpretationId))) return null;
    const saved = serialize(raw);
    return {...saved, verified:false, busy:false, questions:[], choices:{}, status:saved.cancelled ? "cancelled" : "",
      terminal:!saved.interpretationId, error:saved.interpretationId || saved.cancelled ? "" : "未收到口径请求编号。请删除本界面会话以关闭未确认请求，再重新提交。"};
  }

  function create(host) {
    const state = host.state, timers = new Map();
    const alive = (session, conversation) => state.sessionReady && state.backendConversationId === conversation &&
      state.sessions.includes(session) && !host.isClosing(session);
    const post = (path, body) => host.fetchJson(path, {method:"POST", headers:{"Content-Type":"application/json", "X-CSRF-Token":state.csrfToken}, body:JSON.stringify(body)}, 15000);
    const binding = (session, event) => ({conversation_id:state.backendConversationId, display_session_id:session.id, request_id:event.requestId});
    const current = (session, event, body) => alive(session, body.conversation_id) &&
      session.id === body.display_session_id && session.events.includes(event);
    const stop = event => { clearTimeout(timers.get(event)); timers.delete(event); };
    const blocked = session => host.isClosing(session) || session.events.some(e => e.kind === "interpretation" && (e.busy || (e.verified && e.status === "running")));
    function schedule(session, event) {
      stop(event);
      if (host.activeSession() !== session || !event.verified || event.status !== "running" || !current(session, event, binding(session, event))) return;
      timers.set(event, setTimeout(() => {
        timers.delete(event);
        if (host.activeSession() === session) void act(session, event, "resume", true);
      }, 2000));
    }
    function accept(session, event, raw, keepDraft = false) {
      const result = normalize(raw);
      if (!result || (event.interpretationId && event.interpretationId !== result.interpretationId) || result.revision < event.revision) throw {code:"invalid_response"};
      const draft = keepDraft && event.revision === result.revision ? event.draftChoices : {};
      Object.assign(event, result, {verified:true, terminal:false, cancelled:result.status === "cancelled",
        draftChoices:{...result.choices, ...Object.fromEntries(Object.entries(draft || {}).filter(([id, value]) => result.questions.some(q => q.id === id && q.options.some(o => o.id === value))))}});
      if (result.status === "review_ready" && !event.cancelRequested) host.handoff(session, event, result.proposalId);
    }
    async function act(session, event, action, polling = false) {
      // A click during a status read must not disappear. Finish that read, then
      // cancel the server job; never infer cancellation from an aborted fetch.
      if (action === "cancel" && event.polling) {
        event.cancelRequested = true; host.refresh(session); return;
      }
      if (event.busy || !current(session, event, binding(session, event)) || !event.interpretationId || event.cancelled) return;
      if (polling && (host.activeSession() !== session || !event.verified || event.status !== "running")) return;
      const body = binding(session, event);
      if (action === "select") {
        if (!event.verified || !["needs_confirmation", "deferred"].includes(event.status)) return;
        const missing = event.questions.find(q => !q.options.some(o => o.id === event.draftChoices[q.id]));
        if (missing) {
          event.error = "请完成本次全部选择后一次提交。"; host.refresh(session);
          host.container.querySelector(`[data-interpretation-index="${session.events.indexOf(event)}"] [data-question="${missing.id}"]`)?.focus(); return;
        }
        body.revision = event.revision;
        body.choices = Object.fromEntries(event.questions.map(q => [q.id, event.draftChoices[q.id]]));
      }
      stop(event); event.busy = true; event.polling = polling;
      if (!polling) { event.error = ""; host.refresh(session); }
      const revision = event.revision;
      try {
        const raw = await post(`/v1/knowledge-interpretations/${event.interpretationId}/${action}`, body);
        if (!current(session, event, body)) return;
        accept(session, event, raw, action === "resume");
      } catch (error) {
        if (!current(session, event, body)) return;
        event.verified = false; event.terminal = TERMINAL.has(error?.code);
        event.error = ERRORS[error?.code] || "连接中断，尚未确认口径状态。请恢复状态；不会自动重复提交。";
      } finally {
        event.busy = false; event.polling = false;
        if (current(session, event, body)) {
          if (event.cancelRequested) {
            event.cancelRequested = false;
            await act(session, event, "cancel"); return;
          }
          if (!polling || revision !== event.revision || !event.verified) host.refresh(session);
          schedule(session, event);
        }
      }
    }
    async function submit(question) {
      const session = host.activeSession(), conversation = state.backendConversationId;
      if (!question.trim() || question.length > 4000 || state.loading || !alive(session, conversation) || blocked(session) || host.reviewBlocked(session)) return;
      const event = {kind:"interpretation", requestId:host.randomId("meaning"), interpretationId:"", revision:0,
        questions:[], choices:{}, draftChoices:{}, status:"running", busy:true, createdAt:Date.now()};
      session.events.push({kind:"question", question:host.screenQuestion(question), createdAt:Date.now()}, event);
      const body = {...binding(session, event), question};
      host.question.value = ""; host.resetInspector(); host.refresh(session);
      try {
        const raw = await post("/v1/knowledge-interpretations", body);
        if (current(session, event, body)) accept(session, event, raw);
      } catch (error) {
        if (current(session, event, body)) {
          event.terminal = true;
          event.error = ERRORS[error?.code] || "未收到口径请求编号。请删除本界面会话以关闭未确认请求，再重新提交。";
        }
      } finally {
        event.busy = false;
        if (current(session, event, body)) { host.refresh(session); schedule(session, event); }
      }
    }
    async function resumeSession(session) {
      for (const event of session.events) {
        if (host.activeSession() !== session || !state.sessionReady) return;
        if (event.kind !== "interpretation" || !event.interpretationId || event.cancelled || event.terminal) continue;
        if (!event.verified && !event.busy) await act(session, event, "resume");
        else schedule(session, event);
      }
    }
    function clearInput() { for (const event of timers.keys()) stop(event); }
    function render(event, index) {
      const closing = host.isClosing(host.activeSession());
      const editable = event.verified && ["needs_confirmation", "deferred"].includes(event.status) && !event.busy && !closing;
      const status = event.cancelled ? "口径整理已取消" : event.cancelRequested ? "正在取消口径整理" : closing ? "正在关闭口径请求" : event.busy && !event.verified ? "正在提交口径需求" :
        event.terminal ? "口径整理未完成" : !event.verified ? "等待恢复口径状态" :
        ({running:"正在整理口径", needs_confirmation:"请一次完成以下口径选择", deferred:"已保留选择，暂未继续", review_ready:"已生成口径差异", no_change:"未发现口径变更", failed:"口径整理未完成"}[event.status]);
      const fields = event.questions.map(q => `<fieldset class="proposal-field" ${editable ? "" : "disabled"}><legend>${escape(q.prompt)}</legend><div class="proposal-options">${q.options.map(o =>
        `<label class="choice-option"><input type="radio" name="meaning-${index}-${q.id}" data-question="${q.id}" value="${o.id}" ${event.draftChoices[q.id] === o.id ? "checked" : ""} /><span>${escape(o.label)}</span></label>`).join("")}</div></fieldset>`).join("");
      return `<article class="message assistant proposal-message"><div class="message-avatar" aria-hidden="true">${host.avatar()}</div><div class="message-body"><div class="message-meta">皇家小虎取数运行时 · 未查询数据</div>` +
        `<section class="proposal-review interpretation-review" data-interpretation-index="${index}" aria-label="整理业务口径" aria-busy="${event.busy || event.verified && event.status === "running" ? "true" : "false"}">` +
        `<header class="proposal-header"><h3>整理业务口径</h3><span role="status" tabindex="-1">${status}</span></header><p class="proposal-notice">未执行取数，未修改或发布正式口径。</p>` +
        `<form>${fields}<p class="proposal-error" role="alert" ${event.error ? "" : "hidden"}>${escape(event.error || "")}</p><div class="proposal-actions">` +
        (editable ? `<button type="submit" class="primary-button"><svg class="icon" aria-hidden="true"><use href="#icon-check" /></svg>确认全部选择</button>` : "") +
        (!event.verified && event.interpretationId && !event.cancelled && !event.terminal ? `<button type="button" class="secondary-button" data-interpretation-action="resume" ${event.busy || closing ? "disabled" : ""}>恢复口径状态</button>` : "") +
        (event.interpretationId && !event.cancelled && event.status !== "review_ready" ? `<button type="button" class="secondary-button" data-interpretation-action="cancel" ${event.busy || closing ? "disabled" : ""}>取消口径整理</button>` : "") +
        `</div></form></section></div></article>`;
    }
    function bind(container, session) {
      container.querySelectorAll("[data-interpretation-index]").forEach(panel => {
        const event = session.events[Number(panel.dataset.interpretationIndex)];
        panel.querySelector("form").addEventListener("submit", input => {input.preventDefault(); void act(session, event, "select");});
        panel.querySelectorAll("[data-interpretation-action]").forEach(button => button.addEventListener("click", () => void act(session, event, button.dataset.interpretationAction)));
        panel.addEventListener("change", input => {
          if (!event.verified || event.busy || host.isClosing(session) || !["needs_confirmation", "deferred"].includes(event.status)) return;
          const id = input.target.dataset.question, value = input.target.value;
          if (!event.questions.some(q => q.id === id && q.options.some(o => o.id === value))) return;
          event.draftChoices[id] = value; event.error = ""; panel.querySelector(".proposal-error").hidden = true; host.persist();
        });
      });
    }
    return {submit, render, bind, serialize, restore, resumeSession, clearInput, blocked};
  }
  scope.RoyalInterpretation = Object.freeze({create, normalize, serialize, restore});
})(typeof window === "undefined" ? globalThis : window);
