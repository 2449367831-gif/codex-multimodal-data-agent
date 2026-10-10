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
  globalThis.tables = {normalizeBusinessTable, normalizePersistedBusinessTable, formatTableNumber};
  return;
${boundary}`), sandbox);
const api = sandbox.tables;
const fields = [
  ['cargo_owner','货主'],['sales_order_code','订单号'],['order_created_at','订单创建时间'],
  ['customer_code','客户编码'],['customer_name','客户名称'],['warehouse_code','仓库编码'],
  ['warehouse_name','仓库名称'],['sku_code','SKU'],['sku_name','商品'],
  ['request_quantity','原始请求数量'],['quantity_unit_reference','源数量单位代码'],
  ['sales_unit_reference','源销售单位代码'],['order_status','订单状态'],['order_detail_state','明细状态']
];
const fixture = () => ({format:'table', title:'SO07社团销售订单', caption:'测试订单 · 北京时间',
  empty_label:'当前范围没有匹配订单', columns:fields.map(([key,label])=>({key,label})),
  rows:[{cargo_owner:'TEST',sales_order_code:'000012345678901234567890',order_created_at:'2026-09-10 00:00:00',
    customer_code:null,customer_name:null,warehouse_code:'W1',warehouse_name:null,sku_code:'0001',sku_name:null,
    request_quantity:'123456789012345678901234567890.000001',quantity_unit_reference:'010',sales_unit_reference:'040',
    order_status:'0',order_detail_state:'存在范围内有效明细'}],
  summary:[{label:'订单数',value:'1',unit:'单'}], notes:['原始请求数量不换算，不跨单位合计。']});
test('raw quantities and source codes survive display and persisted history',()=>{
  const raw=fixture(); const result=api.normalizeBusinessTable(raw,'completed');
  assert.ok(result); assert.equal(result.columns.length,14);
  assert.equal(result.rows[0].request_quantity,raw.rows[0].request_quantity);
  assert.equal(result.rows[0].quantity_unit_reference,'010');
  assert.equal(result.rows[0].sales_unit_reference,'040');
  assert.equal(api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(result))).rows[0].sales_order_code,raw.rows[0].sales_order_code);
  assert.equal(api.formatTableNumber(result.rows[0].request_quantity),'123,456,789,012,345,678,901,234,567,890.00');
  assert.ok(result.columns.every(c=>!c.unit));
});
test('no detail is not zero and missing codes are not invented',()=>{
  const raw=fixture(); Object.assign(raw.rows[0],{sku_code:null,sku_name:null,request_quantity:null,
    quantity_unit_reference:null,sales_unit_reference:null,order_detail_state:'缺少范围内有效明细'});
  assert.equal(api.normalizeBusinessTable(raw,'completed').rows[0].request_quantity,null);
  raw.rows[0].request_quantity='0'; raw.rows[0].sku_code='0001';
  assert.equal(api.normalizeBusinessTable(raw,'completed').rows[0].request_quantity,'0');
});
for(const [name,mutate] of [
  ['cross-unit total',r=>r.summary.push({label:'合计',value:'5',unit:''})],
  ['false unit',r=>r.summary[0].unit='箱'],
  ['negative',r=>r.rows[0].request_quantity='-1'],
  ['lost decimal precision',r=>r.rows[0].request_quantity=1.25],
  ['missing source unit',r=>delete r.rows[0].sales_unit_reference],
  ['incomplete column group',r=>r.columns.pop()],
  ['unsafe code',r=>r.rows[0].sales_unit_reference='<script>']
]) test(`rejects ${name}`,()=>{
  const raw=fixture(); mutate(raw); assert.equal(api.normalizeBusinessTable(raw,'completed'),null);
});
test('old tables still reject missing SKU',()=>{
  const raw={...fixture(),columns:[{key:'sku_code',label:'SKU'}],rows:[{sku_code:null}],summary:[]};
  assert.equal(api.normalizeBusinessTable(raw,'completed'),null);
});
test('no data is a valid empty table',()=>{
  const raw=fixture(); raw.rows=[]; raw.summary[0].value='0';
  assert.ok(api.normalizeBusinessTable(raw,'completed'));
  assert.equal(api.normalizeBusinessTable(raw,'failed'),null);
});
