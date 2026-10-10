const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const sandbox = {TextEncoder};
vm.createContext(sandbox);
const source = readFileSync('frontend/app.js', 'utf8');
const boundary = '  const elements = {';
assert.equal(source.split(boundary).length, 2);
vm.runInContext(source.replace(boundary, `
  globalThis.tables = {normalizeBusinessTable, normalizePersistedBusinessTable};
  return;
${boundary}`), sandbox);
const api = sandbox.tables;
function fixture() {
  return {format: 'table', table_id: 'model_verified', title: 'Query', caption: '',
    columns: [
      {key: 'col_1', label: 'SKU', kind: 'text'},
      {key: 'col_2', label: 'Quantity', kind: 'text'},
      {key: 'col_3', label: 'Date', kind: 'text'},
      {key: 'col_4', label: 'Note', kind: 'text'},
    ], rows: [{col_1: '001234567890123456789', col_2: '1.2300', col_3: '20260911', col_4: null}]};
}
test('generic identifiers and decimals survive repeated history restoration', () => {
  const raw = fixture();
  let table = api.normalizeBusinessTable(raw, 'completed');
  assert.ok(table);
  for (let index = 0; index < 2; index++) {
    table = api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(table)));
    assert.ok(table);
    assert.equal(table.table_id, 'model_verified');
    assert.equal(JSON.stringify(table.rows), JSON.stringify(raw.rows));
  }
});
test('missing columns cannot be displayed as null', () => {
  const raw = fixture();
  delete raw.rows[0].col_4;
  assert.equal(api.normalizeBusinessTable(raw, 'completed'), null);
});
test('duplicate columns cannot overwrite result values', () => {
  const raw = fixture();
  raw.columns[1].key = 'col_1';
  assert.equal(api.normalizeBusinessTable(raw, 'completed'), null);
});
test('empty generic results remain valid after restore', () => {
  const raw = fixture(); raw.rows = [];
  const table = api.normalizeBusinessTable(raw, 'completed');
  assert.ok(api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(table))));
  assert.equal(api.normalizeBusinessTable(raw, 'failed'), null);
});
test('generic pagination survives history restoration', () => {
  const raw = fixture();
  raw.pagination = {result_id: 'rp_' + 'a'.repeat(32), page: 2, page_count: 2,
    total_rows: 101, start_row: 101, end_row: 101, expires_at: 2000000000, reconciled: true};
  const table = api.normalizeBusinessTable(raw, 'completed');
  const restored = api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(table)));
  assert.ok(restored);
  assert.equal(JSON.stringify(restored.pagination), JSON.stringify(raw.pagination));
  assert.equal(JSON.stringify(restored.rows), JSON.stringify(raw.rows));
});
for (const unit of ['EA/单', '箱/单', '元/单', '元/EA', '元/千克', '单/日', 'EA/周', '件/月']) {
  test(`derived unit ${unit} survives rendering contract and history without conversion`, () => {
    const raw = fixture();
    raw.columns[1] = {key: 'col_2', label: '业务指标', kind: 'number', unit};
    const table = api.normalizeBusinessTable(raw, 'completed');
    assert.ok(table);
    const restored = api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(table)));
    assert.equal(restored.columns[1].unit, unit);
    assert.equal(restored.rows[0].col_2, '1.2300');
  });
}
