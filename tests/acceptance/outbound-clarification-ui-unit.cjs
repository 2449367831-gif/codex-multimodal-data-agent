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
  globalThis.normalize = normalizeClarification;
  return;
${boundary}`), sandbox);

test('document-count label stays executable after API and history normalization', () => {
  const card = {required: true, questions: [{
    id: 'outbound_measure', selection: 'single', prompt: '请选择统计项',
    options: [{id: 'outbound_document_count', label: '出库单数', available: true}]
  }]};
  const normalized = sandbox.normalize(card);
  assert.equal(normalized.required, true);
  assert.equal(normalized.questions[0].options[0].label, '出库单数');
  const restored = sandbox.normalize(JSON.parse(JSON.stringify(normalized)));
  assert.equal(restored.questions[0].options[0].label, '出库单数');
});

test('model wording is not overwritten by a legacy label for the same ID', () => {
  const result = sandbox.normalize({required: true, questions: [{
    id: 'outbound_measure', prompt: '请选择统计项', options: [{id: 'outbound_document_count', label: '按出库单去重计数'}]
  }]});
  assert.equal(result.questions[0].options[0].label, '按出库单去重计数');
});
