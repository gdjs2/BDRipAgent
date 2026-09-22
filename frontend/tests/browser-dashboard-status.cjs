// Dashboard status regression using disposable jobs and queue fixtures only.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async()=>{
  const base='http://127.0.0.1:4180';
  const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4180','--strictPort'],{stdio:'pipe',detached:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const output=process.env.UI_SCREENSHOT_DIR||'/tmp/dashboard-status-ui';
  mkdirSync(output,{recursive:true});
  try {
    for(let i=0;i<100;i++){try{if((await fetch(base)).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
    const task=(type,status,date='2026-09-21T01:00:00Z')=>({id:type,type,status,created_at:date,attempt:1,progress:0,progress_detail:{}});
    const job=(id,state,tasks)=>({id,title:id,source_path:'Movie.mkv',year:2026,analysis_profile:'x264-live',state,tasks,analysis:{},artifacts:[],tracks:[],validation:{},screenshot_policy:{}});
    const jobs=[
      job('CRF waiting','RUNNING_CRF_ANALYSIS',[task('analyze','SUCCEEDED'),task('crf_analysis','QUEUED'),task('review_tracks','SUCCEEDED','2026-09-21T02:00:00Z')]),
      job('Encode waiting','ENCODING',[task('crf_analysis','SUCCEEDED'),task('encode','QUEUED')]),
      job('CRF active','RUNNING_CRF_ANALYSIS',[task('crf_analysis','RUNNING')]),
      job('Encode active','ENCODING',[task('encode','RUNNING')]),
      job('Manual decision','WAITING_FOR_ENCODE_SELECTION',[task('crf_analysis','SUCCEEDED')]),
    ];
    let paused=false;
    const errors=[];
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      let body=[];
      if(path==='/api/config') body={profiles:{},screenshots:{},stages:[]};
      if(path==='/api/jobs') body=jobs;
      if(path==='/api/queue') body={paused,running:[],queued:[],max_encoding_tasks:1,max_other_tasks:3,running_encoding_tasks:1,running_other_tasks:0,effective_encoding_limit:1,effective_other_limit:3};
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto(base);
    const row=id=>page.locator('.job-row').filter({has:page.getByRole('heading',{name:`${id} 2026`,exact:true})});
    await row('CRF waiting').getByText('CRF analysis queued',{exact:true}).waitFor();
    await row('CRF waiting').getByText('Waiting for a CRF analysis slot',{exact:true}).waitFor();
    await row('Encode waiting').getByText('CRF analysis complete · Waiting for an encoding slot',{exact:true}).waitFor();
    await row('CRF active').getByText('CRF analysis running',{exact:true}).waitFor();
    await row('Encode active').getByText('Encoding running',{exact:true}).waitFor();
    assert.equal(await row('CRF waiting').locator('.badge.queued').count(),1);
    assert.equal(await row('Manual decision').locator('.badge.needs-input').count(),1);
    await page.screenshot({path:output+'/dashboard-status-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:output+'/dashboard-status-mobile.png',fullPage:true});
    // Polling must replace queued labels once a worker actually starts.
    jobs[0].tasks.find(t=>t.type==='crf_analysis').status='RUNNING';
    await row('CRF waiting').getByText('CRF analysis running',{exact:true}).waitFor();
    assert.equal(await row('CRF waiting').getByText(/Waiting for a CRF analysis/).count(),0);
    paused=true;
    await row('Encode waiting').getByText(/Queue paused · Resume/).waitFor();
    assert.equal(await row('Encode waiting').locator('.badge.needs-input').count(),1);
    await row('Encode active').getByText('Encoding running',{exact:true}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('PASS: dashboard queued/running/manual states, completed CRF, queue pause, polling and mobile layout.');
  } finally {await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
