import test from 'node:test';
import assert from 'node:assert/strict';
import {taskProgress} from '../src/task-progress.ts';
const task = (extra={}) => ({type:'review_tracks', status:'RUNNING', progress:35, progress_detail:{}, ...extra});
test('unknown-duration work has activity, measured work keeps its actual percentage',()=>{
  assert.equal(taskProgress(task({progress_detail:{indeterminate:true}})).indeterminate,true);
  assert.equal(taskProgress(task()).percent,35);
  assert.equal(taskProgress(task({progress:99})).indeterminate,true);
  assert.equal(taskProgress(task({progress:99,progress_detail:{progress_basis:'work_units'}})).indeterminate,false);
  assert.equal(taskProgress(task({type:'encode',progress:99})).indeterminate,false);
});
test('queued, paused and completed tasks never animate as running',()=>{
  for(const status of ['QUEUED','FAILED','CANCELLED','SUCCEEDED'])
    assert.equal(taskProgress(task({status,progress_detail:{indeterminate:true}})).indeterminate,false);
  assert.equal(taskProgress(task({paused_at:'2026-09-21',progress_detail:{indeterminate:true}})).indeterminate,false);
  assert.equal(taskProgress(task({status:'SUCCEEDED'})).percent,100);
  assert.equal(taskProgress(task({progress:100})).percent,99.9);
  assert.equal(taskProgress(task({status:'QUEUED'})).label,'Queued');
});
