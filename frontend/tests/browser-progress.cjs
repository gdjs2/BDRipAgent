// Progress UI regression with mocked APIs; never starts or alters real jobs.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  const base='http://127.0.0.1:4177';
  const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4177','--strictPort'],{stdio:'pipe',detached:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const output=process.env.UI_SCREENSHOT_DIR || '/tmp/progress-ui';
  mkdirSync(output,{recursive:true});
  try {
    for(let i=0;i<100;i++){try{if((await fetch(base)).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
    let task={id:'task',type:'prepare_tracks',status:'RUNNING',attempt:1,progress:20,created_at:'2026-09-21T01:00:00Z',started_at:'2026-09-21T01:00:00Z',progress_detail:{phase:'Reading subtitle track 4',progress_basis:'work_units',completed_cues:24,total_cues:96}};
    const page=await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/**',route=>{
      const path=new URL(route.request().url()).pathname;
      if(path.endsWith('/events')||path.endsWith('/agent-events')) return route.fulfill({status:200,contentType:'text/event-stream',body:': fixture\n\n'});
      let body=[];
      if(path==='/api/config') body={profiles:{'x264-live':{codec:'x264'}},screenshots:{},stages:[]};
      if(path==='/api/queue') body={running:[],queued:[],paused:false,max_encoding_tasks:1,max_other_tasks:3};
      if(path==='/api/jobs/progress') body={id:'progress',title:'Progress review',source_path:'Movie.mkv',state:'PREPARING_TRACKS',analysis_profile:'x264-live',analysis:{},validation:{},artifacts:[],tasks:[task],tracks:[],screenshot_policy:{},track_analysis_complete:true};
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    async function open(tab='encode') {
      await page.goto(base+'/jobs/progress/'+tab);
      await page.locator('.task-card').waitFor();
    }
    await open();
    let bar=page.getByRole('progressbar',{name:'Task progress',exact:true});
    assert.equal(await bar.getAttribute('value'),'20');
    await page.getByText('20.0%',{exact:true}).waitFor();
    await page.screenshot({path:output+'/progress-measured.png',fullPage:true});
    task={...task,progress_detail:{phase:'Cropping subtitle track 4',progress_basis:'work_units',indeterminate:true}};
    await open();
    assert.equal(await bar.getAttribute('value'),null);
    await page.getByText('Working…',{exact:true}).waitFor();
    assert.equal(await page.getByText('Indeterminate',{exact:true}).count(),0);
    for(const width of [1280,390]) {
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:`${output}/progress-activity-${width}.png`,fullPage:true});
    }
    task={...task,status:'SUCCEEDED',progress:100};
    await open();
    assert.equal(await bar.getAttribute('value'),'100');
    await page.getByText('100.0%',{exact:true}).waitFor();
    task={...task,type:'crf_analysis',status:'RUNNING',progress:83,progress_detail:{stage:'encoding',state:'running',completed:3,total:4,sample_fraction:0.99,flushing:true,frames:120,sample_index:2,samples_per_endpoint:2,codec:'x264',crf:20,indeterminate:true}};
    await open('crf');
    assert.equal(await page.getByRole('progressbar',{name:'CRF analysis progress',exact:true}).getAttribute('value'),null);
    assert.equal(await page.getByRole('progressbar',{name:'Current sample progress',exact:true}).getAttribute('value'),null);
    await page.getByText('Finishing…',{exact:true}).waitFor();
    assert.equal(await page.getByText('99%',{exact:true}).count(),0);
    await page.screenshot({path:output+'/progress-crf-flushing.png',fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Progress UI passed: measured work, activity, completion, CRF flushing, desktop and mobile.');
  } finally {await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
