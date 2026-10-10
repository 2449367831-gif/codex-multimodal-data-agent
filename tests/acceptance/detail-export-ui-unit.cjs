const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const context = {crypto:webcrypto, Uint8Array, TextEncoder, Response};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/detail-export.js','utf8'), context);
const api = context.DetailExport;
const id = 'ex_'+'a'.repeat(32);
const meta = {export_id:id,total_rows:5,chunk_rows:2,chunks:3,expires_at:2000000000};
const hash = async data => Buffer.from(await webcrypto.subtle.digest('SHA-256',data)).toString('hex');
const record = () => ({key:id,exportId:id,total:5,chunkRows:2,index:0,bytes:0,parts:[],pending:null,complete:false});

test('validates metadata and bounded complete-state consistency',()=>{
  assert.equal(api.metadata(meta,id),meta);
  assert.equal(api.checkpoint(record(),meta).index,0);
  for (const change of [{total_rows:100000001},{chunk_rows:50001},{chunks:5},{export_id:'foreign'}])
    assert.throws(()=>api.metadata({...meta,...change},id));
  for (const change of [{index:1},{complete:true},{bytes:10}])
    assert.throws(()=>api.checkpoint({...record(),...change},meta));
});

test('accepts only exact response identity, row boundary and content hash',async()=>{
  const data = new TextEncoder().encode('synthetic,fixture\r\n');
  const headers={'X-Export-Id':id,'X-Chunk-Index':'0','X-Chunk-Rows':'2',
    'X-Loaded-Rows':'2','X-Export-Complete':'false','X-Chunk-SHA256':await hash(data)};
  const good=await api.chunkBytes(new Response(data,{headers}),meta,0);
  assert.equal(good.receipt.rows,2);
  for (const changes of [{'X-Export-Id':'foreign'},{'X-Loaded-Rows':'5'},{'X-Export-Complete':'true'},{'X-Chunk-SHA256':'0'.repeat(64)}])
    await assert.rejects(api.chunkBytes(new Response(data,{headers:{...headers,...changes}}),meta,0));
});

test('committed append with missing browser receipt is recovered without duplication',async()=>{
  const data=new TextEncoder().encode('synthetic content');
  const item=record(); item.pending={bytes:data.length,hash:await hash(data),rows:2};
  item.handle={getFile:async()=>new Blob([data])};
  let saved=0;
  await api.verifiedFile(item,async()=>{saved++;});
  assert.equal(item.index,1);assert.equal(item.bytes,data.length);assert.equal(item.pending,null);assert.equal(saved,1);
  await api.verifiedFile(item,async()=>{throw Error('must not duplicate');});
});

test('uncommitted append retries the same position',async()=>{
  const item=record();item.pending={bytes:3,hash:await hash(new TextEncoder().encode('abc')),rows:2};
  item.handle={getFile:async()=>new Blob([])};
  await api.verifiedFile(item,async()=>{});
  assert.equal(item.index,0);assert.equal(item.pending,null);
});

test('changed file and unexpected tails are never truncated',async()=>{
  const item=record();item.pending={bytes:3,hash:await hash(new TextEncoder().encode('abc')),rows:2};
  item.handle={getFile:async()=>new Blob(['bad'])};
  await assert.rejects(api.verifiedFile(item,async()=>{}),error=>error.code==='file_changed');
  assert.equal(item.index,0);
  item.pending=null;
  await assert.rejects(api.verifiedFile(item,async()=>{}),error=>error.code==='file_changed');
});

test('foreign or malicious error text never reaches the UI',async()=>{
  await assert.rejects(api.chunkBytes(new Response(JSON.stringify({error:{code:'SELECT private password',message:'internal'}}),{status:500}),meta,0),
    error=>error.code==='network_error'&&!error.message.includes('SELECT'));
});
