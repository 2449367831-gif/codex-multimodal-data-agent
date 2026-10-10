const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const sandbox = {console, setTimeout, clearTimeout};
vm.createContext(sandbox);
vm.runInContext(readFileSync('frontend/proposal-review.js', 'utf8'), sandbox);
vm.runInContext(readFileSync('frontend/interpretation.js', 'utf8'), sandbox);
const api = sandbox.RoyalInterpretation;
const fixture = () => ({interpretation_id:'ki_'+'a'.repeat(32), revision:1, expires_at:2000000000,
  state:'needs_confirmation', result_state:'definition_interpretation', process:{state:'not_queried',steps:[]},
  questions:[{id:'q0',prompt:'请选择计量单位',selection:'single',options:[{id:'o0',label:'基本数量'},{id:'o2',label:'箱数'},{id:'defer',label:'暂不确认'}]}],
  choices:{}, proposal:null, error:null});
test('retains all screened decisions including sparse option IDs', () => {
  const raw=fixture(); raw.private='PRIVATE';
  const result=api.normalize(raw);
  assert.equal(result.questions[0].options[1].id,'o2');
  assert.doesNotMatch(JSON.stringify(result),/PRIVATE/);
});
for(const [name,mutate] of [
  ['SQL in label',r=>r.questions[0].options[0].label='SUM(ship_qty)'],
  ['unsafe prompt',r=>r.questions[0].prompt='<script>'],
  ['unknown choice',r=>r.choices.q0='o1'],
  ['duplicate question',r=>r.questions.push(r.questions[0])],
  ['duplicate option',r=>r.questions[0].options.push(r.questions[0].options[0])],
  ['missing defer',r=>r.questions[0].options.pop()],
  ['empty confirmation',r=>r.questions=[]],
  ['fake query',r=>r.process.steps=['doris']],
  ['fake ready',r=>r.state='review_ready'],
  ['unknown state',r=>r.state='completed'],
  ['unsafe error',r=>{r.state='failed';r.error='api_key=PRIVATE';}],
  ['foreign ID',r=>r.interpretation_id='../other']
]) test(`rejects ${name} before rendering`,()=>{const r=fixture();mutate(r);assert.equal(api.normalize(r),null);});
test('history keeps references and enum choices, not model material',()=>{
  const event={...api.normalize(fixture()),requestId:'meaning_test',draftChoices:{q0:'o2'},private:'PRIVATE',createdAt:1};
  const saved=api.serialize(event), restored=api.restore(saved);
  assert.doesNotMatch(JSON.stringify(saved),/PRIVATE|基本数量|请选择|questions/);
  assert.equal(restored.verified,false);assert.equal(restored.draftChoices.q0,'o2');
});
test('creation interrupted before receiving ID cannot silently restart',()=>{
  const restored=api.restore({requestId:'meaning_lost',interpretationId:''});
  assert.equal(restored.terminal,true);assert.match(restored.error,/未收到/);
});
test('ready retains only proposal reference',()=>{
  const r=fixture();r.state='review_ready';r.questions=[];r.proposal={proposal_id:'kp_'+'b'.repeat(32),assessment:'PRIVATE'};
  assert.equal(api.normalize(r).proposalId,r.proposal.proposal_id);
  assert.doesNotMatch(JSON.stringify(api.normalize(r)),/PRIVATE/);
});
