// Deterministic UI coverage for recommendation counts, curation and final choices.
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
(async()=>{
 const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4179','--strictPort'],{stdio:'pipe',detached:true});
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:4179')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  const page=await browser.newPage({viewport:{width:1360,height:1000}}),errors=[],requests=[];
  let count=30, best=0, running=false, strategy="local", manualIds=null, newIds=[];
  const policy=()=>({strategy,best_count:count,count:7,representative:4,encode_challenging:3,min_spacing_seconds:30,min_timeline_bins:3,max_per_scene:1,policy:''});
  const job=()=>({id:'movie',title:'Screenshot review',year:2026,source_path:'Movie.mkv',analysis_profile:'x265-live',state:running?'SCREENSHOT_AGENT_SELECTION':'WAITING_FOR_SCREENSHOT_SELECTION',analysis:{screenshot_more:newIds.length?{requested:15,added:newIds.length,candidate_ids:newIds}:undefined},validation:{},tracks:[],artifacts:[],screenshot_policy:policy(),tasks:running?[{id:'review',type:'select_screenshots',status:'RUNNING',stage:'SCREENSHOT_AGENT_SELECTION',created_at:'2026-09-22T00:00:00Z',progress:0,progress_detail:{},attempt:1}]:[]});
  const gallery=()=>({candidates:40,shortlisted:40,recommended:best,final:0,items:Array.from({length:40},(_,i)=>({id:String(i+1),candidate_id:i+1,shortlisted:true,selected:false,info:{recommendation_rank:manualIds ? (manualIds.includes(i+1)?manualIds.indexOf(i+1)+1:null) : i<best?i+1:null,timeline_seconds:(i+1)*100,source_frame_number:(i+1)*2400,reason:'Character detail'},reservation:i===0?{codec:'x264',job_id:'other',frame_number:2400,spacing_seconds:30}:null})).reverse()});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path.endsWith('/events')||path.endsWith('/agent-events'))return route.fulfill({contentType:'text/event-stream',body:':fixture\n\n'});
   let body=[];
   if(path==='/api/config')body={profiles:{'x265-live':{codec:'x265'}},screenshots:{...policy(),best_count:30},stages:['ENCODING','SCREENSHOT_AGENT_SELECTION','WAITING_FOR_SCREENSHOT_SELECTION']};
   else if(path==='/api/jobs/movie')body=job();
   else if(path==='/api/jobs/movie/screenshots')body=gallery();
   else if(path.endsWith('/strategy')){strategy=route.request().postDataJSON().strategy;body=job();}
   else if(path.endsWith('/best')){manualIds=route.request().postDataJSON().candidate_ids;best=manualIds.length;body=gallery();}
   else if(path.endsWith('/more')){requests.push([path,route.request().postDataJSON()]);newIds=[8,3,12];body={id:'more'};}
   else if(path.endsWith('/best-count')){const data=route.request().postDataJSON();requests.push([path,data]);count=data.best_count;body=job();}
   else if(path.endsWith('/review')){const data=route.request().postDataJSON();requests.push([path,data]);count=data.best_count;running=true;body={id:'review'};}
   else if(path.endsWith('/selection')){requests.push([path,route.request().postDataJSON()]);body=job();}
   else if(path==='/api/queue')body={running:[],queued:[],paused:false,max_encoding_tasks:1,max_crf_tasks:1,max_other_tasks:3,effective_encoding_limit:1,effective_crf_limit:1,effective_other_limit:3,running_encoding_tasks:0,running_crf_tasks:0,running_other_tasks:0};
   await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:4179/jobs/movie/screenshots');
  const input=page.getByRole('spinbutton',{name:'Best screenshot candidates'});
  await page.getByRole('button',{name:'Best 0',exact:true}).click();
  await page.getByText('Your Best list is empty',{exact:true}).waitFor();
  assert.equal(await input.count(),0,'Local mode must not offer automatic ranking');
  await page.getByRole('button',{name:'Browse shortlist',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:/Add candidate .* to best/}).first().getAttribute('aria-label'),'Add candidate 1 to best');
  await page.getByRole('button',{name:'Add candidate 2 to best',exact:true}).click();
  await page.getByRole('button',{name:'Best 1',exact:true}).click();
  assert.equal(await page.getByRole('checkbox',{name:'Choose candidate 2',exact:true}).count(),1);
  assert.equal(await page.getByRole('button',{name:/Refresh best/}).count(),0);
  await page.getByRole('button',{name:'Find 15 more screenshots',exact:true}).click();
  await page.getByRole('button',{name:'Find 15 more screenshots',exact:true}).waitFor();
  assert.equal(requests.at(-1)[1].count,15);
  assert.equal(best,1,'Requesting more must preserve manually added Best frames');
  const batchPanel=page.getByRole('region',{name:'New screenshot batch',exact:true});
  await page.getByRole('button',{name:'New batch (3)',exact:true}).waitFor();
  assert.equal(await batchPanel.getByRole('button',{name:/Add candidate .* to best/}).count(),3);
  assert.equal(await batchPanel.getByRole('button',{name:/Add candidate .* to best/}).first().getAttribute('aria-label'),'Add candidate 3 to best');
  await batchPanel.getByRole('button',{name:'Add candidate 3 to best',exact:true}).click();
  await batchPanel.getByRole('button',{name:'Remove candidate 3 from best',exact:true}).waitFor();
  assert.equal(best,2);
  await page.getByRole('button',{name:'Shortlist (40)',exact:true}).click();
  assert.equal(await page.locator('.review-tile').count(),40);

  manualIds=null;best=30;
  const strategyInput=page.getByLabel('Screenshot selection strategy',{exact:true});
  assert.equal(await strategyInput.inputValue(),'local');
  await strategyInput.selectOption('agent');
  await page.getByText(/The agent visually reviews candidates/).waitFor();
  await page.reload();
  await page.getByRole('button',{name:'Best 30',exact:true}).click();
  assert.equal(await input.inputValue(),'30');
  await input.fill('20');
  await page.getByRole('button',{name:'Save candidate count',exact:true}).click();
  await page.getByText('Candidate count saved for the next review.',{exact:true}).waitFor();
  assert.equal(requests.at(-1)[1].best_count,20);
  assert.equal(await page.getByRole('button',{name:'Best 30',exact:true}).count(),1,'Saving preserves current images');
  await input.fill('41');
  assert.equal(await page.getByRole('button',{name:'Save candidate count',exact:true}).isDisabled(),true);
  await input.fill('20');
  await page.getByRole('button',{name:'Refresh best 20',exact:true}).click();
  await page.getByText(/Screenshot processing is running/).waitFor();
  assert.equal(requests.at(-1)[1].best_count,20);
  assert.equal(await input.isDisabled(),true);
  running=false;best=20;
  await page.reload();
  await page.getByRole('button',{name:'Best 20',exact:true}).waitFor();
  assert.equal(await page.getByRole('checkbox',{name:'Choose candidate 1',exact:true}).isDisabled(),true);
  await page.getByRole('button',{name:'Choose up to 15 available',exact:true}).click();
  assert.equal(await page.getByRole('checkbox',{checked:true}).count(),15);
  await page.getByRole('button',{name:'Clear choices',exact:true}).click();
  for(let i=2;i<=8;i++)await page.getByRole('checkbox',{name:`Choose candidate ${i}`,exact:true}).check();
  await page.getByRole('button',{name:'Confirm 7 & render',exact:true}).click();
  assert.deepEqual(requests.at(-1)[1].candidate_ids,[2,3,4,5,6,7,8]);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('.screenshot-best-settings').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
  assert.deepEqual(errors,[]);
  console.log('Screenshot count browser checks passed: save, refresh, running guard, 20 choices, reserved frames, select seven, bounded select-all, mobile layout.');
 }finally{await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(e=>{console.error(e);process.exitCode=1;});
