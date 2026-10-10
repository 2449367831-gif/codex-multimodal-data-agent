const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const sandbox = {TextEncoder};
vm.createContext(sandbox);
const source = readFileSync('frontend/app.js', 'utf8');
const boundary = '  const elements = {';
assert.equal(source.split(boundary).length, 2);
// Export hoisted pure functions before any DOM setup, without changing production.
vm.runInContext(source.replace(boundary, `
  globalThis.tables = {normalizeBusinessTable, normalizeBusinessTables,
    normalizePersistedBusinessTable, formatTableNumber};
  return;
${boundary}`), sandbox);
const api = sandbox.tables;
const column = (key, label) => ({key, label});
const fixture = () => [
  {format:'table',title:'退货分析订单范围',empty_label:'没有匹配订单',
    columns:[column('cargo_owner','货主'),column('sales_order_code','销售订单'),
      column('order_created','订单创建时间'),column('order_detail_state','明细状态')],
    rows:[{cargo_owner:'TEST',sales_order_code:'000012345678901234567890',
      order_created:'2026-08-01 00:00:00',order_detail_state:'有有效明细'}],
    summary:[{label:'订单数',value:'1',unit:'单'}]},
  {format:'table',title:'订单退货数量核对',empty_label:'没有商品数量',
    columns:[column('cargo_owner','货主'),column('sales_order_code','销售订单'),column('sku_code','SKU'),
      column('order_ship_quantity','订单实发原始数量'),column('returned_quantity','退货原始数量'),
      column('net_sales_quantity','净销售原始数量'),column('return_document_count','关联退货单数'),
      column('sales_detail_count','销售明细行数'),column('return_detail_count','退货明细行数'),column('quantity_state','数量状态')],
    rows:[{cargo_owner:'TEST',sales_order_code:'000012345678901234567890',sku_code:'0001',
      order_ship_quantity:'10',returned_quantity:'12',net_sales_quantity:'-2',
      return_document_count:'1',sales_detail_count:'2',return_detail_count:'1',quantity_state:'退货超过实发'}]}
];
test('accepts both quantity tables and preserves exact signed values through history',()=>{
  const result=api.normalizeBusinessTables(fixture(),'completed');
  assert.ok(result);
  assert.equal(result[1].rows[0].net_sales_quantity,'-2');
  assert.equal(result[0].rows[0].sales_order_code,'000012345678901234567890');
  assert.equal(api.normalizeBusinessTables(JSON.parse(JSON.stringify(result)),'completed',true)[1].rows[0].net_sales_quantity,'-2');
  assert.equal(api.formatTableNumber('-12345678901234567890.25'),'-12,345,678,901,234,567,890.25');
});
test('keeps zero, missing quantity and an order without details distinct',()=>{
  const raw=fixture();
  Object.assign(raw[1].rows[0],{order_ship_quantity:null,net_sales_quantity:null,returned_quantity:'0'});
  const result=api.normalizeBusinessTables(raw,'completed');
  assert.ok(result);
  assert.equal(result[1].rows[0].net_sales_quantity,null);
  assert.equal(result[1].rows[0].returned_quantity,'0');
  raw[1].rows=[];
  assert.equal(api.normalizeBusinessTables(raw,'completed')[0].rows.length,1);
});
for(const key of ['order_ship_quantity','returned_quantity','net_sales_quantity'])
  test(`permits signed ${key}`,()=>{
    const raw=fixture(); raw[1].rows[0][key]='-123.5';
    assert.equal(api.normalizeBusinessTables(raw,'completed')[1].rows[0][key],'-123.5');
  });
for(const key of ['return_document_count','sales_detail_count','return_detail_count'])
  test(`rejects negative ${key} even with a forged flag`,()=>{
    const raw=fixture(); raw[1].rows[0][key]='-1';
    raw[1].columns.find(c=>c.key===key).signed=true;
    assert.equal(api.normalizeBusinessTables(raw,'completed'),null);
  });
for(const [name,mutate] of [
  ['unsafe last row',r=>r[1].rows.push({...r[1].rows[0],quantity_state:'<script>'})],
  ['missing second table',r=>r.pop()],
  ['wrong pair order',r=>r.reverse()],
  ['mixed table schemas',r=>r[0].columns=[column('month','月份'),column('order_count','订单数')]],
  ['non-finite number',r=>r[1].rows[0].net_sales_quantity='Infinity'],
  ['missing required return',r=>r[1].rows[0].returned_quantity=null]
]) test(`rejects entire response: ${name}`,()=>{
  const raw=fixture(); mutate(raw); assert.equal(api.normalizeBusinessTables(raw,'completed'),null);
});
test('does not enable negative numbers for existing sales tables',()=>{
  const raw={...fixture()[1],columns:[{key:'quantity',label:'实际销售出库',signed:true}],rows:[{quantity:'-1'}]};
  assert.equal(api.normalizeBusinessTable(raw,'completed'),null);
});
test('existing DMS two-table contract still works',()=>{
  const base={format:'table',title:'测试结果',empty_label:'没有数据',rows:[]};
  assert.ok(api.normalizeBusinessTables([
    {...base,columns:[column('month','月份'),column('order_count','订单数')]},
    {...base,columns:[column('sales_order_code','订单号'),column('customer_code','客户编码'),column('customer_name','客户名称'),column('created_time','创建时间')]}
  ],'completed'));
});
