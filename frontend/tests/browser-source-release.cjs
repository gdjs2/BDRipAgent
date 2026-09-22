// Browser regression for source-wide release information; APIs are deterministic fixtures.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async()=>{
  const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4175','--strictPort'],{stdio:'pipe',detached:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const output=process.env.UI_SCREENSHOT_DIR||'/tmp/source-release-ui';mkdirSync(output,{recursive:true});
  try {
    for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:4175')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
    const context=await browser.newContext({viewport:{width:1360,height:1050}});
    let details={chinese_name:'夜车',source:'1080p Blu-ray AVC DTS-HD MA 5.1',extra_description:'Original note',movie_description:'[b]Original shared synopsis[/b]',tracker:'https://example.org/announce',upload_screenshots:false};
    let revision=1,sourceId='one',sourceAvailable=true,race=false,generated=false;
    const payloads=[],errors=[];
    const responseJob=id=>({id,title:'The Night Train',year:2026,source_path:'The.Night.Train.mkv',state:generated?'COMPLETE':'ENCODING',analysis_profile:id==='one'?'x265-live':'x264-live',tracks_editable:true,track_analysis_complete:true,analysis:{release_details:details,shared_release_details:{revision,source_job_id:sourceId,title:'The Night Train',source_job_available:sourceAvailable,snapshot_differs:generated},...(generated?{release_result:{task_id:'release',artifacts:[],warnings:[],upload_screenshots:false,package_path:'fixture',infohash:'fixture'}}:{})},tasks:[],artifacts:[],tracks:[],validation:{},screenshot_policy:{}});
    await context.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;
      if(path.endsWith('/events')||path.endsWith('/agent-events'))return route.fulfill({status:200,contentType:'text/event-stream',body:':fixture\n\n'});
      let body;
      if(path==='/api/config')body={profiles:{'x265-live':{codec:'x265'}},screenshots:{},stages:['ANALYZING_SOURCE','ENCODING']};
      else if(path==='/api/queue')body={running:[],queued:[],paused:false,max_encoding_tasks:1,max_crf_tasks:1,max_other_tasks:3,effective_encoding_limit:1,effective_crf_limit:1,effective_other_limit:3,running_encoding_tasks:0,running_crf_tasks:0,running_other_tasks:0};
      else if(/^\/api\/jobs\/(one|two)\/release$/.test(path)){
        const payload=route.request().postDataJSON();payloads.push(payload);
        if(race){race=false;details={...details,chinese_name:'A newer server edit'};revision++;sourceId='two';}
        if(payload.shared_revision!==revision)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'Release information changed in another encoding. Load the shared information before saving.'})});
        const {shared_revision,...saved}=payload;details=saved;revision++;sourceId=path.split('/')[3];body=responseJob(sourceId);
      }else if(/^\/api\/jobs\/(one|two)$/.test(path))body=responseJob(path.split('/')[3]);
      else body=[];
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    const one=await context.newPage(),two=await context.newPage();
    for(const page of [one,two])page.on('pageerror',error=>errors.push(error.message));
    await one.goto('http://127.0.0.1:4175/jobs/one/release');
    await two.goto('http://127.0.0.1:4175/jobs/two/release');
    await one.getByText(/Release information is saved for this source video/).waitFor();
    await two.getByLabel('Movie description (BBCode, optional)').fill('[b]Shared from the x264 encode[/b]');
    await two.getByRole('button',{name:'Save details',exact:true}).click();
    await two.getByText('Release details saved.',{exact:true}).waitFor();
    assert.equal(payloads.at(-1).shared_revision,1);
    await one.waitForFunction(()=>document.querySelector('textarea[rows="10"]')?.value==='[b]Shared from the x264 encode[/b]');
    await one.getByLabel('Movie description (BBCode, optional)').fill('My unsaved description');
    await two.getByLabel('Extra description (optional)').fill('Changed from the other tab');
    await two.getByRole('button',{name:'Save details',exact:true}).click();
    await two.getByText('Release details saved.',{exact:true}).waitFor();
    await one.getByRole('button',{name:'Load shared release information',exact:true}).waitFor();
    assert.equal(await one.getByLabel('Movie description (BBCode, optional)').inputValue(),'My unsaved description');
    assert.equal(await one.getByRole('button',{name:'Save details',exact:true}).isDisabled(),true);
    assert.equal(await one.getByRole('button',{name:'Generate files',exact:true}).isDisabled(),true);
    await one.screenshot({path:output+'/release-conflict-desktop.png',fullPage:true});
    await one.setViewportSize({width:390,height:844});
    await one.screenshot({path:output+'/release-conflict-mobile.png',fullPage:true});
    assert.equal(await one.locator('.release-form').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    await one.getByRole('button',{name:'Load shared release information',exact:true}).click();
    assert.equal(await one.getByLabel('Movie description (BBCode, optional)').inputValue(),'[b]Shared from the x264 encode[/b]');
    assert.equal(await one.getByLabel('Extra description (optional)').inputValue(),'Changed from the other tab');
    await one.getByLabel('Chinese name',{exact:true}).fill('A local edit');
    race=true;
    await one.getByRole('button',{name:'Save details',exact:true}).click();
    await one.getByRole('button',{name:'Load shared release information',exact:true}).waitFor();
    assert.equal(await one.getByLabel('Chinese name',{exact:true}).inputValue(),'A local edit','A rejected stale save must preserve typed input');
    await one.getByRole('button',{name:'Load shared release information',exact:true}).click();
    assert.equal(await one.getByLabel('Chinese name',{exact:true}).inputValue(),'A newer server edit');
    await one.getByRole('button',{name:'Save details',exact:true}).click();
    await one.getByText('Release details saved.',{exact:true}).waitFor();
    sourceAvailable=false;generated=true;
    await one.reload();
    await one.getByText('The Night Train (removed job)',{exact:true}).waitFor();
    await one.getByText(/The existing release files use older information/).waitFor();
    await one.getByRole('heading',{name:'Release files ready',exact:true}).waitFor();
    assert.equal(await one.getByRole('link',{name:'The Night Train',exact:true}).count(),0);
    assert.deepEqual(errors,[]);
    console.log('Source release browser checks passed: shared updates, preserved unsaved edits, stale save recovery, mobile layout, retained output notice.');
  }finally{await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
