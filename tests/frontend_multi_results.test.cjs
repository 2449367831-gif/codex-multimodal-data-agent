const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
const marker = '  const elements = {';
assert.equal(source.split(marker).length, 2);
const context = vm.createContext({TextEncoder});
// Export hoisted pure normalizers before the application binds to the DOM.
vm.runInContext(source.replace(marker,
  '  globalThis.qa = {normalizeBusinessTables}; return;\n' + marker), context);
const normalize = (...args) => context.qa.normalizeBusinessTables(...args);
const table = (title = '仓库汇总') => ({
  format: 'table', table_id: 'model_verified', title, caption: '',
  columns: [{key: 'col_1', label: '仓库编码', kind: 'text'}, {key: 'col_2', label: '仓库数量', kind: 'number'}],
  rows: [{col_1: '00123', col_2: '1.2500'}], empty_label: '当前范围没有可展示的数据'
});

for (const count of [1, 2, 3, 4]) {
  test(`preserves all ${count} generic tables and exact values through refresh`, () => {
    const input = Array.from({length: count}, (_, index) => table(`仓库表${index}`));
    input[count - 1].rows = [];
    const normalized = normalize(input, 'completed');
    assert.equal(normalized.length, count);
    assert.equal(normalized[count - 1].rows.length, 0);
    if (count > 1) {
      assert.equal(normalized[0].rows[0].col_1, '00123');
      assert.equal(normalized[0].rows[0].col_2, '1.2500');
    }
    assert.equal(JSON.stringify(normalize(JSON.parse(JSON.stringify(normalized)), 'completed', true)), JSON.stringify(normalized));
  });
}

test('invalid bundle cannot fall through to one successful table', () => {
  assert.equal(normalize([], 'completed'), null);
  assert.equal(normalize(Array.from({length: 5}, (_, i) => table(`仓库表${i}`)), 'completed'), null);
  assert.equal(normalize([table(), table()], 'completed'), null);
  assert.equal(normalize([table()], 'failed'), null);
  assert.equal(normalize([table(), {...table('明细'), rows: [{col_1: 'SELECT private', col_2: '1'}]}], 'completed'), null);
  assert.equal(normalize([table(), {...table('明细'), table_id: 'untrusted'}], 'completed'), null);
});

test('separate table page identities cannot alias', () => {
  const pagination = {result_id: 'rp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', page: 1,
    page_count: 2, total_rows: 2, start_row: 1, end_row: 1, expires_at: 2000000000, reconciled: true};
  const left = {...table(), pagination};
  const right = {...table('仓库明细'), pagination};
  assert.equal(normalize([left, right], 'completed'), null);
  right.pagination = {...pagination, result_id: 'rp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'};
  assert.equal(normalize([left, right], 'completed').length, 2);
});
