from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


def _frontend_text() -> str:
    return "\n".join(
        (FRONTEND_ROOT / filename).read_text(encoding="utf-8")
        for filename in ("index.html", "app.js", "styles.css")
    )


def test_frontend_renders_only_screened_business_result_without_raw_query_surface() -> None:
    html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
    app_js = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND_ROOT / "styles.css").read_text(encoding="utf-8")

    # The browser may render the screened business_result envelope, but never
    # raw provider output or a copy/preview surface for SQL or database rows.
    forbidden_dom_ids = (
        'id="sqlPreview"',
        'id="copySqlButton"',
        'id="traceRequest"',
        'id="traceTurn"',
    )
    for marker in forbidden_dom_ids:
        assert marker not in html

    forbidden_render_paths = (
        "payload.answer",
        "payload.sql",
        "payload.rows",
        "payload.columns",
        "payload.mcp_calls",
        "payload.usage",
        "payload.knowledge_context",
        "renderMarkdown",
        "extractSql",
        "navigator.clipboard",
    )
    for marker in forbidden_render_paths:
        assert marker not in app_js

    assert "payload?.business_result" in app_js
    assert "payload?.business_table" in app_js
    assert "normalizeBusinessResult" in app_js
    assert "normalizeBusinessTable" in app_js
    assert "renderBusinessResult" in app_js
    assert "renderBusinessTable" in app_js
    assert "business-table" in css
    assert "renderRichText(businessResult.text)" in app_js
    assert "html: false" in app_js
    assert "richTextParser.disable(\"image\")" in app_js
    assert "BUSINESS_RESULT_UNSAFE_PATTERN" in app_js
    assert "业务来源数据未通过完整性校验" in app_js
    assert "BUSY_REQUEST_RETRY_LIMIT" in app_js
    assert 'error.code === "conversation_busy"' in app_js
    assert "transientRetryDelay(attempt, error?.code)" in app_js
    assert "function renderUserQuestion(event)" in app_js
    assert 'event.kind === "question"' in app_js
    assert "user-question-message" in app_js
    assert "displayQuestionText" in app_js
    assert 'outcome: "failed"' in app_js
    assert "updateInspector({" in app_js
    assert "sql-preview" not in css
    assert "data-table" not in css


def test_failed_execution_cards_offer_retry_without_erasing_the_question() -> None:
    app_js = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
    render_execution = app_js.split("function renderExecution(event) {", 1)[1].split(
        "function renderRequestReference", 1
    )[0]
    retry_handler = app_js.split('querySelectorAll("[data-retry-index]")', 1)[1].split(
        'querySelectorAll("[data-feedback-action]")', 1
    )[0]
    assert "renderRetryControl(event, event.index ?? -1)" in render_execution
    assert "renderRequestReference(event.question)" in render_execution
    assert "event.statusMessage || (stopped" in render_execution
    assert 'if (state.loading) return;' in retry_handler
    assert "session.events.splice" not in retry_handler
    assert "reuseQuestion: true" in retry_handler
    assert "requestKey: replayRequest ? event.requestKey" in retry_handler
    assert "taskId: replayRequest ? event.taskId" in retry_handler


def test_frontend_uses_shared_backend_conversation_and_structured_choices() -> None:
    app_js = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")
    html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

    assert "backendConversationId" in app_js
    assert "conversation_id: state.backendConversationId" in app_js
    assert "clarification_answers" in app_js
    assert "option_ids" in app_js
    assert "clarification_pending" in app_js
    assert "question_id" in app_js
    assert "option_id" in app_js
    assert 'id="definitionButton"' in html
    assert 'id="definitionForm"' in html
    assert "/v1/metric-feedback" in app_js
    assert 'id="contextStatus"' in html
    assert 'id="traceCompression"' in html
    assert "safeId(question?.id)" in app_js
    assert "safeId(option?.id)" in app_js
    assert "safeChoiceText" in app_js
    assert "question_${questionIndex + 1}" not in app_js
    assert "questions.length > 12" in app_js
    assert "options.length > 20" in app_js
    assert "parsedClarification?.invalid" in app_js
    assert "safeProcessState" in app_js
    assert "function stageLabel(step)" in app_js
    assert 'if (normalizeKey(step?.id) === "compaction") return;' in app_js
    assert 'if (status === "blocked") return "执行已停止";' in app_js
    assert 'return SAFE_COPY.has(label) ? label : "该选项";' not in app_js
    assert 'if (!label) return null;' in app_js
    # The semantic IDs observed from the Codex gateway have reviewed browser
    # copy; keeping them in the client allow-list prevents a generic fallback.
    for marker in (
        "current_available_actual_outbound",
        "turnover_inventory_point",
        "period_end_finished_goods_snapshot",
        "current_realtime_finished_goods",
        "outbound_basis",
        "actual_completed_outbound",
        "order_requested_quantity",
        "mtd_to_today_00",
        "explicit_period",
        "other_basis",
        "sales_outbound_actual",
        "inventory_available_realtime",
        "inventory_snapshot_quantity",
        "inventory_turnover_finished_goods_snapshot",
        "inventory_turnover_finished_goods_realtime",
        "current_available_realtime",
        "today_historical_snapshot",
        "output_grain",
        "scope",
        "warehouse_sku",
        "all_owner",
        "production_date",
        "in_transit",
        "pickup_availability",
        "snapshot_date",
        "provide_date",
        "sales_window",
        "historical_snapshot_actual_outbound",
        "current_available_order_request",
        "mtd_complete_days",
        "custom_period",
        "last_30_complete_days",
        "previous_natural_month",
        "last_7_complete_days",
        "请选择周转分子；分母固定为所选区间内实际完成销售出库成品的日均数量。",
        "请选择销售时间窗口",
        "区间末日成品库存快照",
        "当前实时成品库存",
        "本月1日至今天00:00",
        "指定开始和结束时间",
        "最近30个完整日",
        "是，按当前可用库存和实际完成出库量",
        "否，使用其他口径，需提交新的业务口径",
        "实际完成出库量",
        "订单请求量",
        "指定开始和结束时间",
    ):
        assert marker in app_js
    assert "historical_turnover_not_released" not in app_js


def test_frontend_persists_only_screened_ui_history_and_never_raw_query_data() -> None:
    app_js = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")

    assert "localStorage.setItem" in app_js
    assert "UI_HISTORY_KEY_PREFIX" in app_js
    assert "persistUiHistory" in app_js
    assert "restoreUiHistory" in app_js
    assert "normalizeStoredEvent" in app_js
    assert "normalizePersistedBusinessResult" in app_js
    assert "normalizePersistedBusinessTable" in app_js
    assert "MAX_PERSISTED_HISTORY_BYTES" in app_js
    assert "businessResult?.text" in app_js
    assert "event.businessTable" in app_js
    assert "sessionStorage.setItem" not in app_js
    assert "localStorage.setItem(\"royal-little-tiger-query-workbench\"" not in app_js
    assert "events.push({ role" not in app_js
    assert "feedbackPayload()" in app_js
    assert "session.events.push({ kind: \"feedback\"" not in app_js
    assert "ensureSharedConversation(payload)" in app_js
    assert "payload.sql" not in app_js
    assert "payload.rows" not in app_js
    assert "payload.columns" not in app_js
    assert "payload.mcp_calls" not in app_js


def test_frontend_recovers_stale_clarification_without_reinserting_the_card() -> None:
    app_js = (FRONTEND_ROOT / "app.js").read_text(encoding="utf-8")

    # Token rotation is an implementation detail, not a reason to make the
    # user repeat a still-valid business selection.
    assert "CLARIFICATION_RECOVERY_CODES" in app_js
    assert "clarificationAcceptsAnswers" in app_js
    assert "postTurnoverReport(session.reportProfileId)" in app_js
    assert "else if (pendingEvent && staleClarificationCleared)" in app_js
    assert "已沿用本次口径选择继续执行" in app_js
    assert "已保留本次口径选择" in app_js
    assert "answerHasOption(retainedSelection" in app_js

    # This copy was misleading: a stale one-time token does not prove that the
    # metric definition or available business choices changed.
    assert "当前口径选择已更新，请重新选择" not in app_js
