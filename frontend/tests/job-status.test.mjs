import test from 'node:test';
import assert from 'node:assert/strict';
import {automaticLogTask, failedTasks, taskPage} from '../src/job-status.ts';
import {hasAnalysisAttempt} from '../src/track-selection.ts';
const task=(id,type,status,created_at)=>({id,type,status,created_at});
const review=task('review','review_tracks','FAILED','2026-09-21T02:00:00Z');
const encode=task('encode','encode','RUNNING','2026-09-21T03:00:00Z');
const retry=task('retry','review_tracks','RUNNING','2026-09-21T04:00:00Z');
test('parallel track failures remain visible while a later encode runs',()=>{
  assert.deepEqual(failedTasks({tasks:[review,encode]}),[review]);
  assert.deepEqual(failedTasks({tasks:[review,encode,retry]}),[]);
});
test('automatic logs follow the page instead of unrelated track attempts',()=>{
  const job={tasks:[review,encode,retry]};
  assert.equal(automaticLogTask(job,'encode'),encode);
  assert.equal(automaticLogTask(job,'tracks'),retry);
  assert.equal(automaticLogTask(job,''),retry);
  assert.equal(automaticLogTask({tasks:[]},'encode'),undefined);
});
test('existing running, failed and cancelled analysis attempts never start again on page mount',()=>{
  for(const status of ['QUEUED','RUNNING','FAILED','CANCELLED','SUCCEEDED'])
    assert.equal(hasAnalysisAttempt({tasks:[{...review,status}]}),true);
  assert.equal(hasAnalysisAttempt({tasks:[]}),false);
  assert.equal(hasAnalysisAttempt({tasks:[task('scan','analyze','SUCCEEDED','')]}),false);
  assert.equal(hasAnalysisAttempt({tasks:[task('scan','analyze','FAILED','')]}),true);
});

test('queue links open the page responsible for each task',()=>{
  assert.equal(taskPage('review_tracks'),'tracks');
  assert.equal(taskPage('crf_analysis'),'crf');
  assert.equal(taskPage('generate_candidates'),'screenshots');
  assert.equal(taskPage('generate_release'),'release');
  assert.equal(taskPage('mux'),'encode');
});

test('queued CRF uses actual task status even when source and track analysis have finished', async()=>{
  const {jobStatus}=await import('../src/job-status.ts');
  const job={state:'RUNNING_CRF_ANALYSIS',tasks:[task('source','analyze','SUCCEEDED','1'),task('crf','crf_analysis','QUEUED','2'),task('review','review_tracks','SUCCEEDED','3')]};
  assert.deepEqual(jobStatus(job),{label:'CRF analysis queued',tone:'queued',detail:'Waiting for a CRF analysis slot'});
  job.tasks[2].status='RUNNING';
  assert.equal(jobStatus(job).label,'CRF analysis queued','Parallel review is not the pipeline status');
  job.tasks[1].status='RUNNING';
  assert.equal(jobStatus(job).label,'CRF analysis running');
  job.tasks[1].progress_detail={state:'complete'};
  assert.equal(jobStatus(job).label,'Saving CRF results');
  job.tasks[1].status='SUCCEEDED';
  assert.equal(jobStatus(job).label,'CRF analysis complete');
});

test('completed CRF is distinguished from queued encoding and manual settings selection',async()=>{
  const {jobStatus}=await import('../src/job-status.ts');
  const tasks=[task('crf','crf_analysis','SUCCEEDED','1'),task('encode','encode','QUEUED','2')];
  const status=jobStatus({state:'ENCODING',tasks});
  assert.equal(status.label,'Encoding queued');
  assert.equal(status.detail,'CRF analysis complete · Waiting for an encoding slot');
  assert.equal(jobStatus({state:'WAITING_FOR_ENCODE_SELECTION',tasks}).tone,'needs-input');
  assert.match(jobStatus({state:'WAITING_FOR_ENCODE_SELECTION',tasks}).detail,/Choose encode settings/);
});

test('held, paused and failed tasks require attention; a queue pause does not stop running tasks',async()=>{
  const {jobStatus}=await import('../src/job-status.ts');
  const queued={...encode,status:'QUEUED'};
  const job={state:'ENCODING',tasks:[queued]};
  assert.match(jobStatus(job,true).detail,/Queue paused/);
  assert.equal(jobStatus(job,true).tone,'needs-input');
  queued.held=true;
  assert.equal(jobStatus(job).label,'Encoding on hold');
  queued.status='RUNNING'; queued.held=false;
  assert.equal(jobStatus(job,true).label,'Encoding running');
  queued.pause_requested=true; queued.paused_at='today';
  assert.equal(jobStatus(job).label,'Encoding paused');
  queued.status='FAILED';
  assert.equal(jobStatus(job).label,'Encoding failed');
  assert.equal(jobStatus({state:'ENCODING',tasks:[queued,{...queued,id:'retry',status:'QUEUED',created_at:'later',paused_at:null}]}).label,'Encoding queued');
  assert.equal(jobStatus({state:'REMUXING',tasks:[task('mux','mux','QUEUED','1')]}).detail,'Waiting for an other-task slot');
});

test('queued release generation explains its single-task limit',async()=>{
  const {jobStatus}=await import('../src/job-status.ts');
  const release=task('release','generate_release','QUEUED','1');
  const job={state:'GENERATING_RELEASE',tasks:[release]};
  assert.equal(jobStatus(job).label,'Release generation queued');
  assert.match(jobStatus(job).detail,/One release at a time/);
  assert.match(jobStatus(job,true).detail,/Queue paused/);
  release.held=true;
  assert.equal(jobStatus(job).label,'Release generation on hold');
});
