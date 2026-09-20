import assert from 'node:assert/strict';
import { test } from 'node:test';
import { applyAgentEvent } from '../src/agent-transcript.ts';

test('live deltas accumulate and final messages replace the same item without duplication', () => {
  let runs = applyAgentEvent([], { type: 'prompt', invocation_id: 'a', stage: 'review', text: 'Choose frames', images: ['1.png'] });
  const original = runs;
  runs = applyAgentEvent(runs, { type: 'delta', invocation_id: 'a', item_id: 'answer', text: 'First ' });
  runs = applyAgentEvent(runs, { type: 'delta', invocation_id: 'a', item_id: 'answer', text: 'second' });
  assert.equal(runs[0].messages[0].text, 'First second');
  assert.equal(original[0].messages.length, 0);
  runs = applyAgentEvent(runs, { type: 'message', invocation_id: 'a', item_id: 'answer', text: 'First second.' });
  assert.deepEqual(runs[0].messages, [{ id: 'answer', text: 'First second.' }]);
  assert.equal(runs[0].prompt, 'Choose frames');
});

test('shortlist and correction requests retain independent prompts and responses', () => {
  let runs = [];
  for (const id of ['first', 'retry']) {
    runs = applyAgentEvent(runs, { type: 'prompt', invocation_id: id, text: id });
    runs = applyAgentEvent(runs, { type: 'message', invocation_id: id, item_id: 'answer', text: id });
  }
  assert.deepEqual(runs.map(r => r.messages[0].text), ['first', 'retry']);
  runs = applyAgentEvent(runs, { type: 'complete', invocation_id: 'retry', text: 'Response received' });
  assert.equal(runs[1].status, 'Response received');
  assert.equal(applyAgentEvent(runs, { type: 'heartbeat' }), runs);
});
