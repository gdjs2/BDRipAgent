const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {mkdirSync}=require('node:fs');
const {chromium}=require('playwright');
(async()=>{
 const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4182','--strictPort'],{stdio:'pipe',detached:true});
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:4182')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  const names=[['subtitle_cleanup','Subtitle cleanup & repair'],['subtitle_alignment','Subtitle alignment'],['subtitle_discovery','Subtitle search'],['subtitle_classification','Subtitle language & SDH'],['track_review','Audio comparison & track flags'],['screenshot_selection','Screenshot selection']];
  let prompts=names.map(([key,title])=>({key,title,description:'Review supplied movie evidence carefully.',text:`Default instructions for ${title}.\nUse the supplied evidence.`,default_text:`Default instructions for ${title}.\nUse the supplied evidence.`,revision:0,customized:false,updated_at:null}));
  const page=await browser.newPage({viewport:{width:1440,height:1100}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',async route=>{
   const url=new URL(route.request().url());let body=[];
   if(url.pathname==='/api/config')body={profiles:{},screenshots:{count:7},stages:[]};
   else if(url.pathname==='/api/agent/prompts')body=prompts;
   else if(url.pathname.startsWith('/api/agent/prompts/')){
    const key=url.pathname.split('/').at(-1),old=prompts.find(p=>p.key===key),data=route.request().postDataJSON();
    if(data.revision!==old.revision)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'This prompt changed in another window. Load the saved version before saving.'})});
    body={...old,text:data.text??old.default_text,customized:data.text!==null,revision:old.revision+1,updated_at:'2026-09-24T15:00:00Z'};
    prompts=prompts.map(p=>p.key===key?body:p);
   }
   return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto('http://127.0.0.1:4182/agent/prompts');
  await page.getByRole('heading',{name:'Agent prompts',exact:true}).waitFor();
  assert.equal(await page.locator('.prompt-task-list button').count(),6);
  const editor=page.getByLabel('Instructions sent to the agent');
  await editor.fill('Fix every subtitle cue.\n保持译名一致。');
  await page.getByRole('button',{name:/Audio comparison/}).click();
  await page.locator('.prompt-editor h2').filter({hasText:'Audio comparison & track flags'}).waitFor();
  await editor.fill('Compare the commentary and main dialogue.');
  await page.getByRole('button',{name:/Subtitle cleanup/}).click();
  await page.locator('.prompt-editor h2').filter({hasText:'Subtitle cleanup & repair'}).waitFor();
  assert.equal(await editor.inputValue(),'Fix every subtitle cue.\n保持译名一致。');
  await page.reload();
  await editor.waitFor();
  assert.equal(await editor.inputValue(),'Fix every subtitle cue.\n保持译名一致。');
  await editor.press('Control+s');
  await page.getByRole('status').filter({hasText:/saved · revision 1/}).waitFor();
  assert.equal(prompts[0].text,'Fix every subtitle cue.\n保持译名一致。');
  await page.getByRole('button',{name:'Restore default',exact:true}).click();
  assert.equal(prompts[0].customized,true,'Restore remains a draft until saved');
  await page.getByRole('button',{name:'Save default prompt',exact:true}).click();
  await page.getByRole('status').filter({hasText:/saved · revision 2/}).waitFor();
  assert.equal(prompts[0].customized,false);
  await editor.fill('Unsaved change');
  prompts[0]={...prompts[0],revision:3,text:'Changed in another window',customized:true};
  await page.getByRole('button',{name:'Save prompt',exact:true}).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await editor.inputValue(),'Unsaved change');
  await page.getByRole('button',{name:'Load saved version',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('textarea').value==='Changed in another window');
  const out=process.env.UI_SCREENSHOT_DIR||'/tmp/agent-prompts-ui';mkdirSync(out,{recursive:true});
  await page.screenshot({path:out+'/desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Editor fits mobile width');
  await page.screenshot({path:out+'/mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);
  console.log('Prompt editor passed: all tasks, drafts, save, reset, conflicts, reload, keyboard shortcut, and responsive layout.');
 }finally{await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
