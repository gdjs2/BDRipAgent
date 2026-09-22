// Encoding target edits against isolated API fixtures; no real jobs are changed.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  const base='http://127.0.0.1:4184';
  const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4184','--strictPort'],{stdio:'pipe',detached:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const output=process.env.UI_SCREENSHOT_DIR || '/tmp/encode-target-ui';
  mkdirSync(output,{recursive:true});
  try {
    for(let i=0;i<100;i++){try{if((await fetch(base)).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
    const profile={codec:'x265',encoder:'x265_10bit',bit_depth:10,preset:'slower',tune:'',crf_min:10,crf_max:26,extra_options:''};
    let task={id:'encode',type:'encode',status:'QUEUED',attempt:1,progress:0,created_at:'2026-09-21T01:00:00Z',progress_detail:{},command_json:[]};
    let job={id:'edit',title:'Encoding target review',source_path:'Movie.mkv',state:'ENCODING',analysis_profile:'x265-live',analysis:{},validation:{},artifacts:[],tasks:[task],tracks:[],screenshot_policy:{},track_analysis_complete:true,tracks_editable:true,encode_config:{data:{codec:'x265',profile:'x265-live',rate_control:'bitrate',bitrate_kbps:8000,profile_snapshot:profile}}};
    let updates=[],retries=0;
    const page=await browser.newPage({viewport:{width:1280,height:960}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/**', async route => {
      const path=new URL(route.request().url()).pathname;
      if(path.endsWith('/events')) return route.fulfill({status:200,contentType:'text/event-stream',body:': fixture\n\n'});
      let body=[];
      if(path==='/api/config') body={profiles:{'x265-live':profile},screenshots:{},stages:[]};
      if(path==='/api/queue') body={running:[],queued:[],paused:false,max_encoding_tasks:1,max_crf_tasks:1,max_other_tasks:3};
      if(path==='/api/jobs/edit') body=job;
      if(path==='/api/jobs/edit/encode-selection') {
        assert.equal(route.request().method(),'PATCH');
        const target=route.request().postDataJSON();updates.push(target);
        const {crf,bitrate_kbps,rate_control,...rest}=job.encode_config.data;
        job.encode_config.data={...rest,...target};body=job;
      }
      if(path==='/api/jobs/edit/reencode') {
        assert.equal(route.request().method(),'POST');
        const target=route.request().postDataJSON();updates.push(target);
        job.encode_config.data={...job.encode_config.data,...target};
        task={...task,id:'replacement',status:'QUEUED',created_at:'2026-09-22T00:00:00Z'};
        job={...job,state:'ENCODING',tasks:[task],reencode:{available:false}};body=job;
      }
      if(path.endsWith('/retry')) {retries++;task={...task,id:'retry',attempt:2,status:'QUEUED'};job.tasks=[task];body=task;}
      if(path==='/api/system/cpu') body={available:false,percent:null,cores:[],logical_cores:0};
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto(base+'/jobs/edit/encode');
    const editor=page.getByRole('region',{name:'Adjust encoding target',exact:true});
    const input=editor.getByLabel('Target video bitrate (Mbps)',{exact:true});
    await input.waitFor();assert.equal(await input.inputValue(),'8');
    await input.fill('12.5');await editor.getByRole('button',{name:'Save encoding target'}).click();
    await editor.getByText('Target saved for the queued encode.',{exact:true}).waitFor();
    assert.deepEqual(updates.at(-1),{rate_control:'bitrate',bitrate_kbps:12500});
    assert.equal(retries,0);assert.equal(task.status,'QUEUED');
    await page.reload();await input.waitFor();assert.equal(await input.inputValue(),'12.5');
    await page.screenshot({path:output+'/encode-edit-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.ok((await editor.locator('form').boundingBox()).height<300,'Mobile fields should not expand into empty vertical space');
    await page.screenshot({path:output+'/encode-edit-mobile.png',fullPage:true});
    task.status='CANCELLED';await page.reload();await input.waitFor();
    await input.fill('10');await editor.getByRole('button',{name:'Save encoding target'}).click();
    await editor.getByText('Target saved. Retry this stage when ready.',{exact:true}).waitFor();
    assert.equal(retries,0);assert.equal(task.status,'CANCELLED');
    await page.getByRole('button',{name:'Retry this stage',exact:true}).click();
    await editor.getByText(/Changes apply when this queued encode starts/).waitFor();
    assert.equal(retries,1);assert.equal(job.encode_config.data.bitrate_kbps,10000);
    await editor.getByLabel('Rate control',{exact:true}).selectOption('crf');
    await editor.getByLabel('CRF',{exact:true}).fill('18.5');
    await editor.getByRole('button',{name:'Save encoding target'}).click();
    await editor.getByText('Target saved for the queued encode.',{exact:true}).waitFor();
    assert.deepEqual(updates.at(-1),{rate_control:'crf',crf:18.5});
    assert.equal(job.encode_config.data.bitrate_kbps,undefined);
    for(const state of [
      {status:'RUNNING'}, {status:'RUNNING',pause_requested:true},
      {status:'RUNNING',cancel_requested:true}, {status:'SUCCEEDED'},
    ]) {
      Object.assign(task,state);await page.reload();await page.getByRole('heading',{name:'Encoding configuration',exact:true}).waitFor();
      assert.equal(await editor.count(),0);
    }
    task.status='SUCCEEDED';job.state='COMPLETE';job.reencode={available:true};
    await page.reload();
    const replacement=page.getByRole('region',{name:'Re-encode video',exact:true});
    await replacement.getByRole('button',{name:'Queue re-encode',exact:true}).waitFor();
    await replacement.getByLabel('Rate control',{exact:true}).selectOption('bitrate');
    await replacement.getByLabel('Target video bitrate (Mbps)',{exact:true}).fill('14');
    await replacement.getByRole('button',{name:'Queue re-encode',exact:true}).click();
    await editor.getByText(/Changes apply when this queued encode starts/).waitFor();
    assert.deepEqual(updates.at(-1),{rate_control:'bitrate',bitrate_kbps:14000});
    assert.equal(task.id,'replacement');
    task.status='QUEUED';job.encode_config.data.execution_mode='smoke';
    await page.reload();await page.getByRole('heading',{name:'Encoding configuration',exact:true}).waitFor();
    assert.equal(await editor.count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS: pending bitrate saves and reloads, canceled jobs stay stopped until retry, target mode switches, running/paused/stopping/smoke jobs cannot edit, completed jobs can queue a replacement, mobile layout fits.');
  } finally {await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
