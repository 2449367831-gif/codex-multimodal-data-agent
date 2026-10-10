const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const sandbox = {console};
vm.createContext(sandbox);
vm.runInContext(readFileSync('frontend/proposal-review.js', 'utf8'), sandbox);
const review = sandbox.RoyalProposalReview;
const id = 'a'.repeat(64);
const options = [{id:'current',label:'保留当前定义'}, {id:'proposed',label:'采用本次提案'}, {id:'defer',label:'暂不确认'}];
function fixture() {
  return {result_state:'definition_review',process:{state:'not_queried',steps:[]},proposal_id:'kp_'+'a'.repeat(32),revision:0,expires_at:2000000000,
    assessment:{state:'proposal_assessed_not_executed',title:'口径变更评估',publication_authorized:false,requires_confirmation:true,
      assessment_complete:true,dependency_state:'已检查声明的依赖关系',choices:{},
      notice:'仅评估本次提案，未查询数据、未发布口径；历史结果不能直接与变更后的口径比较。',
      comparisons:[{decision_id:id,metric:'销售出库',field:'口径说明',before:'当前说明',after:'候选说明'}],
      questions:[{id,prompt:'销售出库：口径说明',selection:'single',options}]}};
}
test('valid projection does not retain arbitrary fields or fake results', () => {
  const raw = fixture(); raw.business_table = {rows:[['PRIVATE_RESULT']]}; raw.assessment.sql = 'PRIVATE_SQL';
  assert.ok(review.normalize(raw)); assert.doesNotMatch(JSON.stringify(review.normalize(raw)), /PRIVATE/);
});
for (const [name, mutate] of [
  ['non-query state', r => r.process.state = 'completed'],
  ['execution steps', r => r.process.steps.push({id:'doris'})],
  ['publication', r => r.assessment.publication_authorized = true],
  ['missing comparison', r => r.assessment.comparisons = []],
  ['duplicate decision', r => {r.assessment.questions.push(r.assessment.questions[0]);r.assessment.comparisons.push(r.assessment.comparisons[0]);}],
  ['question mismatch', r => r.assessment.questions[0].prompt = '请选择库存'],
  ['option mismatch', r => r.assessment.questions[0].options = [{id:'current',label:'使用新规则'}]],
  ['unknown choice', r => r.assessment.choices[id] = 'yes'],
  ['unknown key', r => r.assessment.choices['b'.repeat(64)] = 'current'],
  ['selected without choices', r => {r.revision=1;r.assessment.state='proposal_selected_not_executed';}],
  ['negative revision', r => r.revision=-1],
  ['foreign id', r => r.proposal_id = '../../foreign'],
  ['technical text', r => r.assessment.comparisons[0].after = 'SUM(ship_qty)'],
  ['credential', r => r.assessment.comparisons[0].after = 'sk-privateabcdefghijk'],
  ['unsafe markup', r => r.assessment.comparisons[0].after = '<script>private</script>'],
  ['long text', r => r.assessment.comparisons[0].after = '长'.repeat(241)],
  ['inconsistent deferred state', r => {r.revision=1;r.assessment.state='proposal_deferred_not_executed';r.assessment.choices[id]='current';}]
]) test(`rejects ${name} atomically`, () => {const raw=fixture();mutate(raw);assert.equal(review.normalize(raw),null);});
test('supports 128 decisions without silently slicing to the legacy limit', () => {
  const raw=fixture();
  raw.assessment.questions=Array.from({length:128},(_,i)=>({...raw.assessment.questions[0],id:i.toString(16).padStart(64,'0')}));
  raw.assessment.comparisons=raw.assessment.questions.map(q=>({...raw.assessment.comparisons[0],decision_id:q.id}));
  assert.equal(review.normalize(raw).assessment.comparisons.length,128);
  raw.assessment.questions.push(raw.assessment.questions[0]);raw.assessment.comparisons.push(raw.assessment.comparisons[0]);
  assert.equal(review.normalize(raw),null);
});
test('final and deferred decisions remain distinct', () => {
  const raw=fixture();raw.revision=1;raw.assessment.state='proposal_selected_not_executed';
  raw.assessment.requires_confirmation=false;raw.assessment.choices[id]='current';
  assert.ok(review.normalize(raw));
  raw.assessment.state='proposal_deferred_not_executed';raw.assessment.requires_confirmation=true;raw.assessment.choices[id]='defer';
  assert.ok(review.normalize(raw));
});
test('stored history contains only references and enums, never definitions', () => {
  const event={...review.normalize(fixture()),requestId:'review_test',draftChoices:{[id]:'proposed'},privateInput:'PRIVATE_INPUT',createdAt:1};
  const stored=review.serialize(event);
  assert.doesNotMatch(JSON.stringify(stored),/当前说明|候选说明|PRIVATE|assessment/);
  assert.equal(stored.draftChoices[id],'proposed');
  const restored=review.restore(stored);
  assert.equal(restored.verified,false);assert.equal(restored.assessment,null);
});
test('interrupted creation explicitly needs material again', () => {
  const restored=review.restore({requestId:'review_interrupted',proposalId:''});
  assert.match(restored.error,/原始材料未保留/);
});
