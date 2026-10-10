const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');

function harness(fetchReport = async () => reportResponse()) {
  const nodes = new Map();
  const requests = [];
  const document = {body: {}, getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', hidden: false, disabled: false, dataset: {}, open: false, textContent: '',
      set innerHTML(value) {
        this.markup = value;
        if (id === 'reportSourceSelect') this.value = /value="([^"]+)"/.exec(value)?.[1] || '';
      },
      get innerHTML() { return this.markup || ''; },
      setAttribute() {}, focus() { this.focused = true; },
      showModal() { this.open = true; }, close() { this.open = false; }, reportValidity() { return true; }
    });
    return nodes.get(id);
  }};
  const context = vm.createContext({TextEncoder, URL, document,
    window: {location: {origin: 'http://127.0.0.1:8021'}},
    fetchReport: async (url, options) => {
      requests.push({url, ...JSON.parse(options.body)});
      return fetchReport();
    }
  });
  const marker = '  const proposalHost = {';
  assert.equal(source.split(marker).length, 2);
  vm.runInContext(source.replace(marker, `
    fetchJsonWithRetry = globalThis.fetchReport;
    renderMessages = persistUiHistory = showToast = closeSidebar = updateComposerState = () => {};
    globalThis.qa = {state, elements, reportJobs, canGenerateReportFromEvent, collectReportSources,
      normalizeBusinessTables, normalizeResponse,
      openReportDialog, closeReportDialog, submitReportDialog, generateReport}; return;
  ${marker}`), context);
  context.qa.state.sessionReady = true;
  context.qa.state.backendConversationId = 'conversation-test';
  context.qa.state.csrfToken = 'csrf-test';
  return {qa: context.qa, nodes, requests};
}

function query(extra = {}) {
  return {kind: 'execution', requestId: 'a'.repeat(32), outcome: 'completed',
    queryStatus: 'succeeded', resultState: 'live_success', question: 'Example query',
    createdAt: 1000, businessTable: {rows: [{quantity: '12.50'}]}, ...extra};
}

function reportResponse() {
  const file = 'rpf_' + 'a'.repeat(32);
  return {status: 'completed', report_id: 'rpt_' + 'a'.repeat(32), title: 'Example report',
    audience: 'Operations', table_count: 1, row_count: 1,
    files: [{file_id: file, format: 'html', filename: 'report.html', content_type: 'text/html',
      url: '/v1/reports/files/' + file}]};
}

function addSession(qa, event = query(), id = 'view-1') {
  const session = {id, title: id, events: [event]};
  qa.state.sessions.push(session);
  qa.state.activeId = id;
  return session;
}

test('report entry is directly below new conversation and wired to the report dialog', () => {
  assert.match(html, /id="newChatButton"[\s\S]*?<\/button>\s*<button[^>]*id="reportEntryButton"/);
  assert.match(html, /id="reportEntryButton"[^>]*aria-controls="reportDialog"/);
  assert.match(source, /elements.reportEntryButton.addEventListener\("click", openReportDialog\)/);
  assert.match(source, /elements.reportForm.addEventListener\("submit", submitReportDialog\)/);
});

test('only successful populated query results are report sources', () => {
  const {qa} = harness();
  assert.equal(qa.canGenerateReportFromEvent(query()), true);
  for (const resultState of ['summary_only', 'batch_complete']) {
    assert.equal(qa.canGenerateReportFromEvent(query({resultState})), true);
  }
  for (const change of [
    {queryStatus: 'truncated_result'}, {queryStatus: 'query_failed'}, {outcome: 'failed'},
    {resultState: 'no_data'}, {requestId: ''}, {businessTable: {rows: []}}, {kind: 'question'}
  ]) assert.equal(qa.canGenerateReportFromEvent(query(change)), false);
  assert.equal(qa.canGenerateReportFromEvent(query({businessTable: null,
    businessTables: [{rows: [{value: '1'}]}]})), true);
});

test('reviewed rule turnover summary and detail tables remain reportable', () => {
  const {qa} = harness();
  const center = {
    format: 'table', title: '经营中心7日周转（分仓14日）', caption: '规则取数结果',
    empty_label: '当前没有可展示的经营中心汇总',
    columns: [
      {key: 'warehouse', label: '经营中心', kind: 'text'},
      {key: 'current_stock', label: '可用成品库存＋在途', kind: 'number', unit: 'EA'},
      {key: 'daily_sales', label: '近7天日均销量', kind: 'number', unit: 'EA/日'},
      {key: 'turnover_text', label: '周转天数', kind: 'text'}
    ],
    rows: [{warehouse: '中心仓', current_stock: '100', daily_sales: '10.00', turnover_text: '10.00'}], summary: []
  };
  const detail = {
    format: 'table', title: '各分仓14日周转', caption: '规则取数结果',
    empty_label: '当前没有可展示的分仓周转明细',
    columns: [
      {key: 'belonging_center', label: '归属中心仓', kind: 'text'},
      {key: 'warehouse', label: '分仓', kind: 'text'},
      {key: 'current_stock', label: '可用成品库存＋在途', kind: 'number', unit: 'EA'},
      {key: 'daily_sales', label: '近14天日均销量', kind: 'number', unit: 'EA/日'},
      {key: 'turnover_text', label: '周转天数', kind: 'text'}
    ],
    rows: [{belonging_center: '中心仓', warehouse: '分仓A', current_stock: '50', daily_sales: '5.00', turnover_text: '10.00'}], summary: []
  };
  const tables = qa.normalizeBusinessTables([center, detail], 'completed');
  assert.equal(tables.length, 2);
  const normalized = qa.normalizeResponse({request_id: 'a'.repeat(32), outcome: {state: 'completed'},
    query_status: 'succeeded', result_state: 'live_success', business_tables: [center, detail]});
  assert.equal(normalized.outcome, 'completed');
  assert.equal(normalized.businessTables.length, 2);
  assert.equal(normalized.resultState, 'succeeded');
  assert.equal(qa.canGenerateReportFromEvent({kind: 'execution', requestId: normalized.requestId,
    outcome: normalized.outcome, queryStatus: normalized.queryStatus, resultState: normalized.resultState,
    businessTables: normalized.businessTables}), true);
});

test('newest sources are first and the current conversation is preselected', () => {
  const {qa} = harness();
  const first = addSession(qa, query({createdAt: 5000}), 'older-view');
  const current = addSession(qa, query({createdAt: 2000, requestId: 'b'.repeat(32)}), 'current-view');
  assert.equal(qa.collectReportSources()[0].session, first);
  qa.openReportDialog();
  const selected = qa.collectReportSources().find(x => x.key === qa.elements.reportSourceSelect.value);
  assert.equal(selected.session, current);
  assert.equal(qa.elements.reportDialog.open, true);
});

test('empty state offers a query action and never calls the report endpoint', () => {
  const {qa, requests} = harness();
  qa.openReportDialog();
  assert.equal(qa.elements.reportSourceEmpty.hidden, false);
  assert.equal(qa.elements.reportSubmit.hidden, true);
  assert.equal(qa.elements.reportQueryButton.hidden, false);
  assert.equal(qa.elements.reportTitleInput.disabled, true);
  assert.equal(requests.length, 0);
});

test('sidebar report passes custom instructions to the existing endpoint and displays downloads', async () => {
  const {qa, requests} = harness();
  const session = addSession(qa);
  qa.openReportDialog();
  qa.elements.reportTitleInput.value = 'Inventory review';
  qa.elements.reportAudienceInput.value = 'Operations';
  qa.elements.reportInstructionsInput.value = 'Highlight exceptions';
  await qa.submitReportDialog({preventDefault() {}});
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0], {url: '/v1/reports', conversation_id: 'conversation-test',
    request_id: session.events[0].requestId, title: 'Inventory review', audience: 'Operations',
    instructions: 'Highlight exceptions', formats: ['html', 'pdf', 'word', 'excel', 'csv', 'png']});
  assert.equal(session.events[0].report.report_id, reportResponse().report_id);
  assert.equal(qa.elements.reportDialog.open, true);
  assert.equal(qa.elements.reportDialogStatus.dataset.state, 'completed');
  assert.equal(qa.elements.reportDialogDownloads.hidden, false);
  assert.match(qa.elements.reportDialogDownloads.innerHTML, /\/v1\/reports\/files\/rpf_/);
  assert.equal(qa.reportJobs.size, 0);
});

test('failure remains visible, preserves the form and permits retry', async () => {
  let fail = true;
  const {qa, requests} = harness(async () => {
    if (fail) throw new Error('connection failed');
    return reportResponse();
  });
  const session = addSession(qa);
  qa.openReportDialog();
  qa.elements.reportInstructionsInput.value = 'Exceptions only';
  await qa.submitReportDialog({preventDefault() {}});
  assert.equal(qa.elements.reportDialogStatus.dataset.state, 'failed');
  assert.equal(qa.elements.reportSubmit.disabled, false);
  assert.equal(qa.elements.reportInstructionsInput.value, 'Exceptions only');
  assert.equal(session.events[0].report, undefined);
  fail = false;
  await qa.submitReportDialog({preventDefault() {}});
  assert.equal(requests.length, 2);
  assert.equal(qa.elements.reportDialogStatus.dataset.state, 'completed');
});

test('report jobs deduplicate by request, not by the message index across conversations', async () => {
  let resolve;
  const gate = new Promise(r => { resolve = r; });
  const {qa, requests} = harness(async () => { await gate; return reportResponse(); });
  const left = addSession(qa);
  const right = addSession(qa, query({requestId: 'b'.repeat(32)}), 'view-2');
  const first = qa.generateReport(left, left.events[0], 0);
  const duplicate = await qa.generateReport(left, left.events[0], 0);
  const second = qa.generateReport(right, right.events[0], 0);
  assert.ok(duplicate.error);
  assert.equal(requests.length, 2);
  assert.equal(qa.reportJobs.size, 2);
  resolve();
  await Promise.all([first, second]);
  assert.equal(qa.reportJobs.size, 0);
});

test('closing and reopening the dialog retains an in-flight report without another request', async () => {
  let resolve;
  const gate = new Promise(r => { resolve = r; });
  const {qa, requests} = harness(async () => { await gate; return reportResponse(); });
  addSession(qa);
  qa.openReportDialog();
  const job = qa.submitReportDialog({preventDefault() {}});
  assert.equal(qa.elements.reportSubmit.disabled, true);
  qa.closeReportDialog();
  qa.openReportDialog();
  assert.equal(qa.elements.reportDialog.open, true);
  assert.equal(qa.elements.reportSubmit.disabled, true);
  assert.equal(requests.length, 1);
  resolve();
  await job;
  assert.equal(qa.elements.reportDialogDownloads.hidden, false);
});
