// Measured CPU widget and on-demand logs; all API requests are mocked.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  const base='http://127.0.0.1:4179';
  const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4179','--strictPort'],{stdio:'pipe',detached:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const output=process.env.UI_SCREENSHOT_DIR || '/tmp/system-ui';
  mkdirSync(output,{recursive:true});
  try {
    for(let i=0;i<100;i++){try{if((await fetch(base)).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
    const task={id:'encode',type:'encode',status:'RUNNING',attempt:1,progress:30,created_at:'2026-09-21T01:00:00Z',started_at:'2026-09-21T01:00:00Z',progress_detail:{phase:'Encoding',progress_basis:'work_units'}};
    const job={id:'system',title:'System review',source_path:'Movie.mkv',state:'ENCODING',analysis_profile:'x264-live',analysis:{},validation:{},artifacts:[],tasks:[task,{...task,id:'review',type:'review_tracks',lane:'tracks'}],tracks:[],screenshot_policy:{},track_analysis_complete:false,tracks_editable:true};
    const page=await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    let logReads=0,agentReads=0,cpuAvailable=true,cpuPercent=42,cpuFrequency=3250,metricsAvailable=true;
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;
      if(path.endsWith('/agent-events')) {agentReads++;return route.fulfill({status:200,contentType:'text/event-stream',body:': fixture\n\n'});}
      if(path.endsWith('/events')) return route.fulfill({status:200,contentType:'text/event-stream',body:': fixture\n\n'});
      let body=[];
      if(path==='/api/config') body={profiles:{'x264-animation':{codec:'x264',tune:'animation'},'x264-live':{codec:'x264'},'x265-animation':{codec:'x265',tune:'animation'},'x265-live':{codec:'x265'}},screenshots:{count:7,representative:4,min_spacing_seconds:30},stages:[]};
      if(path==='/api/queue') body={running:[],queued:[],paused:false,max_encoding_tasks:1,max_other_tasks:3};
      if(path==='/api/jobs/system') body=job;
      if(path.endsWith('/logs')) {logReads++;body={text:'Measured encoder output'};}
      if(path==='/api/system/cpu') body={available:cpuAvailable,percent:cpuPercent,logical_cores:96,frequency_mhz:cpuAvailable&&metricsAvailable?cpuFrequency:null,load_average:cpuAvailable&&metricsAvailable?{one_minute:18.25,five_minutes:15.1,fifteen_minutes:12.5}:null,sampled_at:await page.evaluate(()=>new Date().toISOString()),cores:Array.from({length:96},(_,id)=>({id,percent:id%2?100:20}))};
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto(base+'/jobs/system/encode');
    const monitor=page.getByRole('region',{name:'CPU monitor',exact:true});
    await monitor.getByText('42%',{exact:true}).waitFor();
    assert.equal(await page.locator('.task-log details').getAttribute('open'),null);
    assert.equal(logReads,0);
    await page.locator('.task-log summary').click();
    await page.getByLabel('Task log output').getByText('Measured encoder output',{exact:true}).waitFor();
    await page.locator('.task-log summary').click();
    const pausedLogReads=logReads;
    await page.waitForTimeout(3300);
    assert.equal(logReads,pausedLogReads,'Folded logs must stop polling');
    await page.getByRole('button',{name:'Expand CPU monitor',exact:true}).click();
    assert.equal(await page.getByRole('meter',{name:'Overall CPU utilization',exact:true}).getAttribute('aria-valuenow'),'42');
    assert.equal(await page.locator('.cpu-core').count(),96);
    const load=page.getByRole('group',{name:'System load averages',exact:true});
    await load.getByText('18.25',{exact:true}).waitFor();
    assert.deepEqual(await load.locator('dt').allTextContents(),['1 min','5 min','15 min']);
    assert.deepEqual(await load.locator('dd').allTextContents(),['18.25','15.10','12.50']);
    await page.getByLabel('Average CPU frequency',{exact:true}).getByText('3.25 GHz',{exact:true}).waitFor();
    cpuFrequency=2450;
    cpuPercent=67;
    await page.waitForFunction(()=>document.querySelector('[aria-label="Overall CPU utilization"]').getAttribute('aria-valuenow')==='67');
    await page.getByLabel('Average CPU frequency',{exact:true}).getByText('2.45 GHz',{exact:true}).waitFor();
    await page.clock.install();
    const loads=[12,18,24,29.9,30,45,62,69.9,70,88,97,100,92,81,68,52,34,26,19,12,21,38,46,58,75,83,91,72,67];
    for(const load of loads) {
      cpuPercent=load;
      await page.clock.runFor(2100);
      await page.waitForFunction(value=>[...document.querySelectorAll('.cpu-history-column')].some(column=>Number(column.dataset.value)===value),load);
    }
    assert.equal(await page.locator('.cpu-history-column').count(),30);
    // Color belongs to each height band, not the sample's overall utilization.
    for(const [load, counts] of [[30,[6,0,0]],[70,[6,8,0]],[100,[6,8,6]]]) {
      cpuPercent=load;
      await page.clock.runFor(2100);
      await page.waitForFunction(value=>[...document.querySelectorAll('.cpu-history-column')].some(column=>Number(column.dataset.value)===value),load);
      const column=page.locator(`.cpu-history-column[data-value="${load}"]`).last();
      const actual=await Promise.all(['low','medium','high'].map(level=>column.locator(`.cpu-history-block.cpu-load-${level}`).count()));
      assert.deepEqual(actual,counts,`${load}% must fill green, then amber, then coral blocks`);
      const colors=await column.locator('.cpu-history-block').evaluateAll(blocks=>[...new Set(blocks.map(block=>getComputedStyle(block).fill))]);
      assert.equal(colors.length,counts.filter(count=>count>0).length,'Block classes must render distinct colors');
    }
    const chart=page.getByRole('img',{name:/Overall CPU usage over the last 60 seconds/});
    await chart.focus();await chart.press('End');await chart.press('ArrowLeft');
    assert.ok(await page.locator('.cpu-history-reading time').count());
    await chart.press('Escape');
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await page.locator('.cpu-history-column').first().evaluate(el=>getComputedStyle(el).transitionDuration),'0s');
    await page.emulateMedia({reducedMotion:'no-preference'});
    const before=await monitor.boundingBox();
    await page.getByRole('button',{name:'Move CPU monitor',exact:true}).press('ArrowLeft');
    assert.ok((await monitor.boundingBox()).x < before.x);
    const handle=await page.getByRole('button',{name:'Move CPU monitor',exact:true}).boundingBox();
    await page.mouse.move(handle.x+handle.width/2,handle.y+handle.height/2);
    await page.mouse.down();await page.mouse.move(30,40,{steps:5});await page.mouse.up();
    let bounds=await monitor.boundingBox();
    assert.ok(bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=1281&&bounds.y+bounds.height<=901);
    await page.screenshot({path:output+'/cpu-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(100);
    bounds=await monitor.boundingBox();
    assert.ok(bounds.x>=0&&bounds.x+bounds.width<=390&&bounds.y+bounds.height<=844);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:output+'/cpu-mobile.png',fullPage:true});
    await page.getByRole('button',{name:'Collapse CPU monitor',exact:true}).click();
    await page.reload();
    await page.getByRole('button',{name:'Expand CPU monitor',exact:true}).waitFor();
    assert.equal(await page.locator('.cpu-core').count(),0,'Folded preference must survive reload');
    await page.goto(base+'/jobs/system/tracks');
    await page.locator('.agent-transcript summary').first().waitFor();
    assert.equal(agentReads,0,'Folded agent logs must not open an event stream');
    await page.locator('.agent-transcript summary').first().click();
    await page.waitForFunction(()=>document.querySelector('.agent-transcript details').open);
    await page.waitForTimeout(200);
    assert.ok(agentReads>0);
    await page.locator('.agent-transcript summary').first().click();
    metricsAvailable=false;
    await page.clock.runFor(2100);
    await page.getByRole('button',{name:'Expand CPU monitor',exact:true}).click();
    await page.getByLabel('Average CPU frequency',{exact:true}).getByText('Unavailable',{exact:true}).waitFor();
    assert.deepEqual(await load.locator('dd').allTextContents(),['—','—','—']);
    assert.ok(await page.getByRole('meter',{name:'Overall CPU utilization',exact:true}).getAttribute('aria-valuenow'));
    await page.getByRole('button',{name:'Collapse CPU monitor',exact:true}).click();
    cpuAvailable=false;
    await page.clock.runFor(2100);
    await page.getByRole('button',{name:'Expand CPU monitor',exact:true}).click();
    await monitor.getByText('CPU data unavailable').first().waitFor();
    assert.equal(await page.getByRole('meter',{name:'Overall CPU utilization',exact:true}).getAttribute('aria-valuenow'),null);
    await page.clock.runFor(62000);
    assert.equal(await page.locator('.cpu-history-column[data-value]').count(),0,'Unavailable history ages out instead of displaying invented zeros');
    await page.goto(base+'/new');
    const primary=page.getByLabel('Analysis & encode profile',{exact:true});
    const secondary=page.getByLabel('Second encode profile',{exact:true});
    assert.equal(await primary.inputValue(),'x265-live');
    assert.equal(await secondary.inputValue(),'x264-live');
    await primary.selectOption('x264-live');
    assert.equal(await secondary.inputValue(),'x265-live');
    await secondary.selectOption('x265-animation');
    await primary.selectOption('x264-animation');
    assert.equal(await secondary.inputValue(),'x265-animation','Keep an explicit compatible profile choice');
    assert.deepEqual(errors,[]);
    console.log('PASS: live load averages and average frequency, independent unavailable metrics; block history colors, time gaps, keyboard inspection, reduced motion and live profile defaults;  folded logs stop polling/streaming; real CPU values update; 96-core grid fits desktop/mobile; drag, keyboard, collapse persistence and unavailable state work.');
  } finally {await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
