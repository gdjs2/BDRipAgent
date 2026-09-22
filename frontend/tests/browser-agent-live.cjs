const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { mkdirSync } = require('node:fs');
const { chromium } = require('playwright');
(async () => {
 const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4176','--strictPort'],{stdio:'pipe',detached:true});
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:4176')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   window.EventSource=class extends EventTarget {
    constructor(url){super();this.url=url;window.agentStream=this;queueMicrotask(()=>this.onopen?.());}
    close(){this.closed=true;}
   };
  });
  const base={job_id:'movie',task_id:'task',title:'Star Wars: A New Hope',year:1977,profile:'x264-live',task_status:'RUNNING',created_at:'2026-09-22T12:15:00Z',invocation_id:'review',stage:'Subtitle cleanup'};
  const prompt='Review and repair the supplied subtitle cues. Use source dialogue and alternate-language references to verify the damaged line. Preserve timing and report uncertainty.\n\n'+Array.from({length:45},(_,i)=>`Cue ${i+1}: A source dialogue reference for this scene.`).join('\n');
  const initial=[{...base,event_id:10,type:'prompt',text:prompt},{...base,event_id:11,type:'message',item_id:'answer',text:'I matched the damaged cue against the English source track. The surrounding dialogue confirms the speaker and scene.\n\nI’m checking an alternate translation before applying the repair.'}];
  let replay=false;
  await page.route('**/api/**',route=>{
   const url=new URL(route.request().url());let body={};
   if(url.pathname==='/api/config')body={profiles:{},screenshots:{count:7},stages:[]};
   else if(url.pathname==='/api/agent/history')body=url.searchParams.has('before_id')?{events:[{...base,invocation_id:'older',event_id:2,type:'prompt',task_status:'SUCCEEDED',stage:'Screenshot review',text:'Select the strongest comparison frames.'},{...base,invocation_id:'older',event_id:3,type:'message',item_id:'a',task_status:'SUCCEEDED',text:'Selected 20 candidates with varied lighting and scene composition.'}],cursor:15,before_id:2,has_more:false}:{events:replay?[...initial,{...base,event_id:12,type:'delta',item_id:'answer',text:'\n\nReference checked. Repair applied.'},{...base,event_id:13,type:'complete',text:'Subtitle repair complete'}]:initial,cursor:replay?13:11,before_id:10,has_more:true};
   return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:4176/agent');
  await page.getByRole('link',{name:/Agent Live/}).waitFor();
  await page.getByText('Streaming response',{exact:true}).waitFor();
  assert.equal(await page.locator('.agent-live-state.running').count(),1);
  await page.locator('.agent-live-prompt summary').click();
  await page.getByText(/Cue 45:/).last().waitFor();
  await page.locator('.agent-live-prompt summary').click();
  await page.evaluate(event=>window.agentStream.dispatchEvent(new MessageEvent('agent_activity',{data:JSON.stringify(event)})),{...base,event_id:12,type:'delta',item_id:'answer',text:'\n\nReference checked. Repair applied.'});
  await page.getByText(/Reference checked. Repair applied./).waitFor();
  // Repeated delivery on reconnect must not duplicate a response chunk.
  await page.evaluate(event=>window.agentStream.dispatchEvent(new MessageEvent('agent_activity',{data:JSON.stringify(event)})),{...base,event_id:12,type:'delta',item_id:'answer',text:'\n\nReference checked. Repair applied.'});
  assert.equal((await page.locator('.agent-live-response pre').textContent()).match(/Repair applied/g).length,1);
  await page.evaluate(event=>window.agentStream.dispatchEvent(new MessageEvent('agent_activity',{data:JSON.stringify(event)})),{...base,event_id:13,type:'complete',text:'Subtitle repair complete'});
  await page.getByText('Complete',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Load earlier conversations',exact:true}).click();
  await page.getByText('Select the strongest comparison frames.',{exact:true}).waitFor();
  assert.equal(await page.locator('.agent-live-conversation').count(),2);
  await page.getByRole('button',{name:/Follow.*live/i}).click();
  const out=process.env.UI_SCREENSHOT_DIR||'/tmp/agent-live-ui';mkdirSync(out,{recursive:true});
  await page.screenshot({path:out+'/desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Mobile view must not overflow');
  await page.screenshot({path:out+'/mobile.png',fullPage:true});
  replay=true;await page.reload();
  await page.getByText('Subtitle repair complete',{exact:true}).waitFor();
  assert.equal(await page.locator('.agent-live-response pre').count(),1);
  assert.equal(await page.evaluate(()=>window.agentStream.url),'/api/agent/events?after=13');
  assert.deepEqual(errors,[]);
  console.log('Agent Live browser checks passed: navigation, prompts, streaming, replay, history, responsive layout.');
 }finally{await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
