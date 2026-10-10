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
  globalThis.batches = {normalizeDetailMetadata, normalizeDetailSelection, normalizeBusinessTable,
    normalizePersistedBusinessTable, normalizeResultState};
  return;
${boundary}`), sandbox);
const api = sandbox.batches;
const meta = () => ({result_id:'db_'+'B'.repeat(32),next_batch:1,loaded_rows:0,total_rows:100001,complete:false,expires_at:2000000000});
const table = () => ({format:'table',title:'范围汇总',caption:'全部仓库',empty_label:'没有数据',
  columns:[{key:'quantity',label:'实发数量'},{key:'outbound_count',label:'出库单数'}],
  rows:[{quantity:'2',outbound_count:'1'}],summary:[],notes:[],detail_batches:meta()});
test('continuation metadata survives repeated history restoration',()=>{
  let value=api.normalizeBusinessTable(table(),'completed');
  for(let index=0;index<3;index++) value=api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(value)));
  assert.equal(JSON.stringify(value.detail_batches),JSON.stringify(meta()));
});
for(const [name,change] of [
  ['foreign identifier',m=>m.result_id='rr_'+'B'.repeat(32)],
  ['negative count',m=>m.loaded_rows=-1],
  ['unsafe integer',m=>m.total_rows=Number.MAX_SAFE_INTEGER+1],
  ['false completion',m=>m.complete=true],
  ['skipped initial batch',m=>m.next_batch=2],
  ['boolean count',m=>m.next_batch=true],
  ['unreadable loaded state',m=>m.loaded_rows=1],
])test(`rejects invalid batch metadata: ${name}`,()=>{
  const raw=table();change(raw.detail_batches);assert.equal(api.normalizeBusinessTable(raw,'completed'),null);
});
test('browser selection retains IDs only, never plan or scope',()=>{
  const selection=api.normalizeDetailSelection({result_id:meta().result_id,batch_index:2,scope:'foreign',sql:'private'});
  assert.equal(JSON.stringify(selection),JSON.stringify({result_id:meta().result_id,batch_index:2}));
  assert.equal(api.normalizeDetailSelection({result_id:meta().result_id,batch_index:true}),null);
});
test('partial batches remain distinct from whole-request completion',()=>{
  assert.equal(api.normalizeResultState('completed','batch_complete'),'batch_complete');
  assert.equal(api.normalizeResultState('failed','batch_complete'),'');
});
