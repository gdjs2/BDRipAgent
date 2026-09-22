// Opt-in interactions against isolated acceptance jobs, never production jobs.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
(async()=>{
  const base=process.env.APP_BASE_URL || 'http://127.0.0.1:18081';
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const output=process.env.UI_SCREENSHOT_DIR || '/review/interactions';
  fs.mkdirSync(output,{recursive:true});
  let pausedId;
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  try {
    const login=await context.request.post(base+'/api/session',{data:{token:process.env.UI_TEST_TOKEN}});
    assert.equal(login.ok(),true);
    const jobs=await (await context.request.get(base+'/api/jobs')).json();
    assert(jobs.every(job=>job.title.startsWith('E2E TEST ')), 'This test only changes isolated E2E jobs');
    const page=await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    const running=jobs.find(job=>job.tasks.some(task=>task.type==='encode' && task.status==='RUNNING' && task.can_pause));
    if(running && process.env.TEST_PAUSE==='1') {
      await page.goto(base+`/jobs/${running.id}/encode`);
      await page.getByRole('button',{name:'Pause encoding',exact:true}).click();
      pausedId=running.tasks.find(task=>task.type==='encode' && task.status==='RUNNING').id;
      await page.getByRole('button',{name:'Resume encoding',exact:true}).waitFor();
      const read=async()=> (await (await context.request.get(base+`/api/jobs/${running.id}`)).json()).tasks.find(t=>t.id===pausedId);
      const before=await read();assert(before.paused_at,'Worker must acknowledge the pause');
      await page.waitForTimeout(2500);
      const after=await read();assert.equal(after.progress,before.progress);
      await page.screenshot({path:output+'/encoding-paused.png'});
      await page.getByRole('button',{name:'Resume encoding',exact:true}).click();
      await page.getByRole('button',{name:'Pause encoding',exact:true}).waitFor();
      assert.equal((await read()).pause_requested,false);pausedId=undefined;
      console.log('PASS: real encoder pause acknowledgement, frozen progress, and resume');
    }
    const complete=jobs.find(job=>job.state==='COMPLETE');
    assert(complete,'A completed acceptance job is required');
    for(const [size,width,height] of [['desktop',1440,1000],['mobile',390,844]]) {
      await page.setViewportSize({width,height});
      await page.goto(base+`/jobs/${complete.id}/release`);
      await page.getByRole('heading',{name:'Release files ready'}).waitFor();
      for(const name of ['BBCODE','NFO','MD5','Encoder notes']) {
        const button=page.getByRole('button',{name,exact:true});
        if(await button.count()) {
          await button.click();
          await page.locator('.release-text-preview pre').waitFor();
          assert((await page.locator('.release-text-preview pre').innerText()).trim().length>0);
        }
      }
      await page.getByRole('button',{name:'BBCODE',exact:true}).click();
      await page.screenshot({path:output+`/release-preview-${size}.png`,fullPage:true});
      await page.goto(base+`/jobs/${complete.id}/artifacts`);
      await page.getByRole('heading',{name:'Generated artifacts'}).waitFor();
      await page.getByRole('button',{name:'Release',exact:true}).click();
      await page.getByLabel('Find a file').fill('.torrent');
      assert.equal(await page.locator('.artifact-row').count(),1);
      await page.locator('.artifact-row summary').click();
      assert((await page.locator('.artifact-row code').innerText()).includes('[ART]'));
      await page.goto(base+`/jobs/${complete.id}/screenshots`);
      await page.locator('.shot-card').first().waitFor();
      await page.locator('.shot-card').first().click();
      await page.getByRole('dialog',{name:'Screenshot comparison'}).waitFor();
      assert.equal(await page.getByRole('dialog').getByRole('checkbox').count(),0, 'Final comparisons must not expose draft selection controls');
      await page.waitForFunction(()=>[...document.querySelectorAll('.comparison')].length===2 && [...document.querySelectorAll('.comparison')].every(img=>img.complete&&img.naturalWidth>0));
      await page.screenshot({path:output+`/comparison-${size}.png`});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.getByRole('button',{name:'Close ×',exact:true}).click();
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: real release text previews, artifact filtering and paths, and full-resolution comparison dialogs on desktop/mobile');
  } finally {
    if(pausedId) await context.request.post(base+`/api/tasks/${pausedId}/resume`,{data:{}});
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
