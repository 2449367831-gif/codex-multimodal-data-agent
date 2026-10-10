const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
const marker = '  const proposalHost = {';
assert.equal(source.split(marker).length, 2);
const context = vm.createContext({TextEncoder, URL,
  window: {location: {origin: 'http://127.0.0.1:8021'}},
  document: {body: {}, getElementById: () => ({})}});
vm.runInContext(source.replace(marker, `
  globalThis.qa = {normalizeResponse, normalizeStoredEvent, serializeUiEvent,
    normalizeClarification, renderClarification, renderExecution, stageLabel}; return;
${marker}`), context);
const qa = context.qa;
const payload = () => ({status: 'needs_selection', outcome: {state: 'clarification_required'},
  query_status: 'clarification_required', result_state: 'clarification_required',
  clarification: {required: true, token: 'ct_' + 'a'.repeat(32), draft_id: 'cd_' + 'b'.repeat(64), questions: [{
    id: 'two_c_basis', prompt: '按哪个业务标识识别2C订单？', selection: 'single', options: [
      {id: 'shipment_fulfillment', label: '出库单履约为2C', available: true},
      {id: 'sales_order_channel', label: '销售订单渠道为2C', available: true}]}]},
  // Also exercise receipts emitted by older server versions.
  process: {state: 'waiting_for_selection', steps: [{id: 'context', state: 'fresh'},
    {id: 'query', state: 'pending'}, {id: 'compaction', state: 'not_needed'}]}});

test('new model choices are displayed, not converted to blocked data results', () => {
  const value = qa.normalizeResponse(payload());
  assert.equal(value.outcome, 'clarification_required');
  assert.equal(value.statusMessage, '');
  const question = value.clarification.questions[0];
  assert.equal(question.id, 'two_c_basis');
  assert.equal(question.prompt, '按哪个业务标识识别2C订单？');
  assert.deepEqual(Array.from(question.options, o => o.id), ['shipment_fulfillment', 'sales_order_channel']);
  const html = qa.renderClarification({kind: 'clarification', ...value}, 0);
  assert.match(html, /出库单履约为2C/);
  assert.match(html, /销售订单渠道为2C/);
  assert.doesNotMatch(html, /本次未返回数据|未形成可确认|结果已校验/);
  assert.deepEqual(Array.from(value.process, s => [s.id, s.state]),
    [['planning', 'completed'], ['clarify', 'pending']]);
});

test('model IDs, wording, selection and disabled flags survive history round-trips', () => {
  const raw = payload();
  const question = raw.clarification.questions[0];
  question.id = 'business_basis:new-v2';
  question.prompt = '请选择出库单的统计范围';
  question.selection = 'multiple';
  question.options[0] = {id: 'by_document_v2', label: '按出库单统计', available: true};
  question.options[1].available = false;
  let event = {kind: 'clarification', sourceQuestion: '查询昨天2C销售出库',
    ...qa.normalizeResponse(raw), selectedAnswers: [{question_id: question.id, option_ids: ['by_document_v2']}]};
  for (let i = 0; i < 3; i++) {
    event = qa.normalizeStoredEvent(JSON.parse(JSON.stringify(qa.serializeUiEvent(event))));
    const current = event.clarification.questions[0];
    assert.equal(current.id, question.id);
    assert.equal(current.prompt, question.prompt);
    assert.equal(current.multiple, true);
    assert.equal(current.options[1].available, false);
    assert.equal(event.selectedAnswers[0].option_ids[0], 'by_document_v2');
    assert.equal(event.clarification.token, '');
  }
});

for (const bad of ['SELECT x FROM ywzt_ods.secret', '<img src=x onerror=alert(1)>',
    'password=secret', 'sk-' + 'a'.repeat(30), 'ywzt_ods.shipment']) {
  test(`unsafe clarification text remains blocked: ${bad.slice(0, 12)}`, () => {
    const raw = payload();
    raw.clarification.questions[0].options[0].label = bad;
    let event = {kind: 'execution', question: '测试问题', index: 0, ...qa.normalizeResponse(raw)};
    for (let i = 0; i < 2; i++) {
      assert.equal(event.clarification || null, null);
      assert.equal(event.resultState, 'clarification_error');
      const html = qa.renderExecution(event);
      assert.match(html, /口径选项展示失败/);
      assert.doesNotMatch(html, /本次未返回数据|结果已校验|data-retry-index|未形成可确认/);
      event = qa.normalizeStoredEvent(JSON.parse(JSON.stringify(qa.serializeUiEvent(event))));
    }
  });
}

test('IDs are never rewritten to positional values that break token validation', () => {
  const raw = payload();
  raw.clarification.questions[0].id = 'invalid id';
  assert.equal(qa.normalizeResponse(raw).resultState, 'clarification_error');
});
