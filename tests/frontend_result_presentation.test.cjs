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
    renderExecution, resultLabel, stageLabel, normalizeProcess}; return;
${marker}`), context);
const qa = context.qa;
const artifact = {artifact_id: 'art_' + 'a'.repeat(32), kind: 'center_warehouse_turnover_7d',
  content_type: 'image/png', title: '中心仓与分仓周转', period: '中心7天；分仓14天',
  url: '/v1/artifacts/art_' + 'a'.repeat(32)};
const table = () => ({format: 'table', table_id: 'center_warehouse_turnover_7_14',
  title: '经营中心7日周转（分仓14日）', caption: '', empty_label: '当前没有可展示的经营中心汇总',
  columns: [
    {key: 'warehouse', label: '经营中心', kind: 'text', align: 'left'},
    {key: 'current_stock', label: '可用成品库存＋在途', kind: 'number', align: 'right', unit: 'EA'},
    {key: 'daily_sales', label: '近7天日均销量', kind: 'number', align: 'right', unit: 'EA/日'},
    {key: 'turnover_text', label: '周转天数', kind: 'text', align: 'right'}],
  rows: [{warehouse: '测试中心', current_stock: '140', daily_sales: '10', turnover_text: '14.00'}],
  summary: [], notes: []});
const response = (extra = {}) => ({outcome: {state: 'completed'}, query_status: 'succeeded',
  result_state: 'live_success', business_table: table(), artifacts: [artifact],
  process: {steps: [{id: 'context', state: 'completed'}, {id: 'query', state: 'completed'},
    {id: 'compaction', state: 'not_due'}]}, ...extra});
const event = (payload) => ({kind: 'execution', question: '生成中心仓和分仓14天周转数据图片',
  index: 0, ...qa.normalizeResponse(payload)});
const refresh = (value) => qa.normalizeStoredEvent(JSON.parse(JSON.stringify(qa.serializeUiEvent(value))));

test('turnover table, report and terminal status survive repeated refreshes', () => {
  let value = event(response({message: '本次未形成可确认的实时数据结果，请重新提交'}));
  for (let i = 0; i < 3; i++) {
    assert.equal(value.outcome, 'completed');
    assert.equal(value.queryStatus, 'succeeded');
    assert.equal(value.businessTable.rows[0].current_stock, '140');
    assert.equal(value.artifacts.length, 1);
    assert.equal(value.statusMessage, '');
    assert.equal(value.process.length, 2);
    const html = qa.renderExecution(value);
    assert.match(html, /查询完成，结果已通过校验/);
    assert.match(html, /近7天日均销量/);
    assert.doesNotMatch(html, /execution-status-message|data-retry-index|正在校验结果/);
    value = refresh(value);
  }
});

for (const cause of ['label', 'row', 'missing', 'bundle', 'artifact']) {
  test(`presentation rejection (${cause}) cannot become query failure or SQL retry`, () => {
    const raw = response();
    if (cause === 'label') raw.business_table.columns[0].label = '未审核显示列';
    if (cause === 'row') raw.business_table.rows[0].warehouse = 'SELECT private FROM internal';
    if (cause === 'missing') { delete raw.business_table; raw.artifacts = []; }
    if (cause === 'bundle') raw.business_tables = [];
    if (cause === 'artifact') raw.artifacts = [{...artifact, url: 'https://example.com/private'}];
    let value = event(raw);
    for (let i = 0; i < 3; i++) {
      assert.equal(value.queryStatus, 'succeeded');
      assert.equal(value.outcome, 'failed');
      assert.equal(value.resultState, 'presentation_error');
      const html = qa.renderExecution(value);
      assert.match(html, /查询已完成，结果展示异常/);
      assert.match(html, /页面格式校验/);
      assert.doesNotMatch(html, /结果已通过校验|本次未形成|data-retry-index|SELECT private/);
      assert.equal(value.artifacts.length, ['artifact', 'missing'].includes(cause) ? 0 : 1);
      value = refresh(value);
    }
  });
}

test('repair legacy contradictory history using its valid report, without a new query', () => {
  const legacy = {kind: 'execution', outcome: 'failed', queryStatus: 'succeeded',
    resultState: 'succeeded', question: '生成周转图片', businessTable: null,
    artifacts: [artifact], process: [{id: 'query', state: 'completed'}]};
  const value = qa.normalizeStoredEvent(legacy);
  assert.equal(value.outcome, 'completed');
  assert.equal(value.artifacts.length, 1);
  assert.doesNotMatch(qa.renderExecution(value), /execution-status-message|data-retry-index/);
  for (const invalid of [[], [{...artifact, url: 'https://example.com/private'}]]) {
    assert.equal(qa.normalizeStoredEvent({...legacy, artifacts: invalid}).outcome, 'failed');
  }
});

for (const status of ['query_failed', 'query_rejected', 'query_unavailable', 'truncated_result', 'invalid_result']) {
  test(`backend ${status} cannot be promoted by a valid report or table`, () => {
    let value = event(response({query_status: status, result_state: 'presentation_error'}));
    for (let i = 0; i < 3; i++) {
      assert.equal(value.queryStatus, status);
      assert.equal(value.outcome, 'failed');
      assert.equal(value.resultState, status);
      assert.equal(value.businessTable, null);
      assert.equal(value.artifacts.length, 0);
      assert.doesNotMatch(qa.renderExecution(value), /结果已通过校验|近7天日均销量|<img[^>]+\/v1\/artifacts/);
      value = refresh(value);
    }
  });
}

test('successful empty result is not failed and does not prompt SQL retry', () => {
  const raw = response({query_status: 'empty_result', artifacts: []});
  raw.business_table.rows = [];
  const value = refresh(event(raw));
  assert.equal(value.outcome, 'completed');
  assert.match(qa.renderExecution(value), /当前条件无数据/);
  assert.doesNotMatch(qa.renderExecution(value), /查询执行失败|data-retry-index/);
});

test('conflicting backend outcome does not claim business validation succeeded', () => {
  const value = event(response({outcome: {state: 'failed'}}));
  assert.equal(value.businessTable, null);
  assert.equal(value.artifacts.length, 0);
  assert.doesNotMatch(qa.renderExecution(value), /结果已通过校验/);
});

test('completed stage labels and persisted steps cannot revert to running', () => {
  const steps = [{id: 'query', state: 'completed'}, {id: 'compaction', state: 'not_due'}];
  assert.equal(JSON.stringify(qa.normalizeProcess(steps, 'completed')),
    JSON.stringify([{id: 'query', state: 'completed'}]));
  for (const id of ['query', 'route', 'routing', 'preparing', 'finalizing']) {
    assert.doesNotMatch(qa.stageLabel({id, state: 'completed'}), /正在/);
  }
  assert.equal(qa.stageLabel({id: 'validation', state: 'not_due'}), '结果已校验整理');
});
