const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4183','--strictPort'],{stdio:'pipe',detached:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:4183')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
    let history=[], request, reject=false;
    let job={id:'fixture',title:'Subtitle guidance fixture',source_path:'Movie.mkv',state:'ENCODING',tracks_editable:true,track_analysis_complete:true,analysis_profile:'x265-live',analysis:{track_review_version:1},validation:{},tasks:[],artifacts:[],screenshot_policy:{},tracks:[],subtitle_uploads:[{id:'upload1',task_id:'review1',filename:'Movie.zh-Hans.srt',language:'zh-Hans',status:'FAILED',detail:{phase:'Aligning subtitles'},error:'Timing alignment needs more evidence'}]};
    const page=await browser.newPage({viewport:{width:1440,height:1050}}), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;let body=[];
      if(path.endsWith('/events')||path.endsWith('/agent-events'))return route.fulfill({status:200,contentType:'text/event-stream',body:': ready\n\n'});
      if(path==='/api/config')body={profiles:{'x265-live':{codec:'x265'}},screenshots:{},stages:[]};
      else if(path==='/api/jobs/fixture')body=job;
      else if(path.endsWith('/subtitle-guidance'))body=history;
      else if(path.endsWith('/continue-subtitle-review')){
        request=route.request().postDataJSON();
        if(reject)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'Another track review is running'})});
        history=[...history,{id:history.length+1,created_at:'2026-09-24T17:00:00Z',...request}];
        job={...job,subtitle_uploads:[{...job.subtitle_uploads[0],task_id:'review2',status:'QUEUED',error:null,detail:{phase:'Continuing with guidance'}}]};body={id:'review2'};
      }
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto('http://127.0.0.1:4183/jobs/fixture/tracks');
    await page.locator('.subtitle-upload > summary').click();
    await page.getByText('Add guidance & continue review',{exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'Continue with guidance',exact:true}).isDisabled(),true);
    await page.getByLabel('Message to the subtitle agent').fill('Use source dialogue to repair the damaged text. 保留简体中文。');
    await page.reload();
    await page.locator('.subtitle-upload > summary').click();
    assert.equal(await page.getByLabel('Message to the subtitle agent').inputValue(),'Use source dialogue to repair the damaged text. 保留简体中文。');
    await page.getByLabel('Re-review all cues with this guidance').check();
    reject=true;
    await page.getByRole('button',{name:'Continue with guidance',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'Another track review'}).waitFor();
    assert.match(await page.getByLabel('Message to the subtitle agent').inputValue(),/source dialogue/);
    const out='/tmp/subtitle-guidance-ui';mkdirSync(out,{recursive:true});
    await page.screenshot({path:out+'/desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile panel fits the viewport');
    await page.screenshot({path:out+'/mobile.png',fullPage:true});
    reject=false;
    await page.getByRole('button',{name:'Continue with guidance',exact:true}).click();
    await page.getByText('Continuing with guidance',{exact:true}).waitFor();
    assert.deepEqual(request,{message:'Use source dialogue to repair the damaged text. 保留简体中文。',recheck_completed:true});
    await page.getByText('Your review guidance (1)',{exact:true}).click();
    await page.getByText(request.message,{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Continue with guidance',exact:true}).count(),0,'Active reviews cannot be continued again');
    assert.deepEqual(errors,[]);
    console.log('Subtitle guidance: draft persistence, message submission, re-review option, error recovery, history, active-state controls, and responsive layout passed.');
  } finally {await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
