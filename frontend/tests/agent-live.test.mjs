import assert from 'node:assert/strict';
import { test } from 'node:test';
import { applyLiveEvent } from '../src/agent-live.ts';
const base = {event_id:1,job_id:'job',task_id:'task',title:'Movie',year:2026,profile:'x265-live',task_status:'RUNNING',created_at:'2026-09-22T12:00:00Z',invocation_id:'run'};
test('global stream keeps prompts, merges deltas, and replaces final answers without duplication', () => {
  let runs = applyLiveEvent([], {...base,type:'prompt',text:'Review these subtitles',stage:'Subtitle cleanup'});
  runs=applyLiveEvent(runs,{...base,type:'delta',item_id:'a',text:'Hello '});
  runs=applyLiveEvent(runs,{...base,type:'delta',item_id:'a',text:'world'});
  assert.equal(runs[0].messages[0].text,'Hello world');
  runs=applyLiveEvent(runs,{...base,type:'message',item_id:'a',text:'Hello world.'});
  runs=applyLiveEvent(runs,{...base,type:'complete',text:'Reviewed'});
  assert.equal(runs[0].messages.length,1);
  assert.equal(runs[0].prompt,'Review these subtitles');
  assert.equal(runs[0].state,'complete');
});
test('task failures end running conversations, and invocation IDs are isolated by task', () => {
  let runs=applyLiveEvent([],{...base,type:'prompt',text:'one'});
  runs=applyLiveEvent(runs,{...base,task_id:'two',type:'prompt',text:'two'});
  runs=applyLiveEvent(runs,{...base,type:'task_end',task_status:'FAILED',error:'Disconnected'});
  assert.deepEqual(runs.map(r=>r.state),['error','running']);
  assert.equal(runs[0].status,'Disconnected');
  assert.equal(runs[1].prompt,'two');
});
test('historical prompts from stopped tasks do not appear as still responding',()=>{
  const runs=applyLiveEvent([],{...base,type:'prompt',text:'old',task_status:'CANCELLED'});
  assert.equal(runs[0].state,'error');
});
