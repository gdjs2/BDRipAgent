// Real HLS playback against tests/container_media_preview.py; all media are synthetic.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  const origin=process.env.MEDIA_TEST_API || 'http://127.0.0.1:18766';
  const headers={Authorization:'Bearer test-token-with-more-than-24-characters'};
  const jobs=await (await fetch(origin+'/api/jobs',{headers})).json();
  const id=jobs[0].id;
  const server=spawn('npm',['run','preview','--','--host','127.0.0.1','--port','4178','--strictPort'],{stdio:'pipe',detached:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox','--autoplay-policy=no-user-gesture-required']});
  const output=process.env.UI_SCREENSHOT_DIR||'/tmp/media-preview-ui';mkdirSync(output,{recursive:true});
  try {
    for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:4178')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
    const context=await browser.newContext({viewport:{width:1360,height:1050}});
    const errors=[], requests=[];
    await context.route('**/api/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname.endsWith('/events')||url.pathname.endsWith('/agent-events'))return route.fulfill({status:200,contentType:'text/event-stream',body:':fixture\n\n'});
      if(url.pathname.includes('/playback/'))requests.push(url.pathname+url.search);
      const response=await route.fetch({url:origin+url.pathname+url.search,headers,timeout:120000});
      await route.fulfill({response});
    });
    const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error')console.error(m.text());});
    page.setDefaultTimeout(20000);
    await page.goto(`http://127.0.0.1:4178/jobs/${id}/`);
    await page.getByText('12.35 Mbps',{exact:true}).waitFor();
    await page.goto(`http://127.0.0.1:4178/jobs/${id}/encode`);
    await page.getByLabel('Encoder information',{exact:true}).waitFor();
    assert.match(await page.getByLabel('Encoder information',{exact:true}).innerText(),/frame I: 20/);
    await page.goto(`http://127.0.0.1:4178/jobs/${id}/artifacts`);
    await page.getByRole('button',{name:'Open video preview',exact:true}).click();
    const video=page.getByLabel('Final video preview',{exact:true});
    await page.waitForFunction(()=>document.querySelector('video')?.readyState>=2);
    console.log('Loaded',await video.evaluate(v=>({time:v.currentTime,duration:v.duration,ready:v.readyState})));
    await video.evaluate(v=>{v.muted=true;void v.play();});
    try {await page.waitForFunction(()=>document.querySelector('video')?.currentTime>1);} catch(error) {console.log('Stalled',await video.evaluate(v=>({time:v.currentTime,duration:v.duration,ready:v.readyState,paused:v.paused,error:v.error?.message,buffered:Array.from({length:v.buffered.length},(_,i)=>[v.buffered.start(i),v.buffered.end(i)])})));throw error;}
    await video.evaluate(v=>{v.pause();v.currentTime=7.4;});
    await page.getByLabel('Subtitles',{exact:true}).selectOption('5');
    await page.waitForFunction(()=>{const v=document.querySelector('video');return v?.readyState>=2&&v.currentTime>=7;});
    assert.ok(await video.evaluate(v=>v.paused),'Changing tracks while paused stays paused');
    await video.evaluate(v=>{void v.play();});
    await page.waitForFunction(()=>document.querySelector('video')?.currentTime>7.7);
    await video.evaluate(v=>v.pause());
    const red=await video.evaluate(v=>{const c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;const ctx=c.getContext('2d');ctx.drawImage(v,0,0);return [...ctx.getImageData(5,5,1,1).data];});
    assert.ok(red[0]>200&&red[2]<50);
    await page.getByLabel('Video track',{exact:true}).selectOption('1');
    await page.getByLabel('Audio track',{exact:true}).selectOption('3');
    await page.waitForFunction(()=>{const v=document.querySelector('video');return v?.readyState>=2&&v.currentTime>=7;});
    const blue=await video.evaluate(v=>{const c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;const ctx=c.getContext('2d');ctx.drawImage(v,0,0);return [...ctx.getImageData(5,5,1,1).data];});
    assert.ok(blue[2]>200&&blue[0]<50);
    await page.getByLabel('Playback speed',{exact:true}).selectOption('2');
    await video.evaluate(v=>{v.currentTime=11;void v.play();});
    await page.waitForFunction(()=>document.querySelector('video')?.currentTime>13);
    await page.getByLabel('Subtitles',{exact:true}).selectOption('4');
    await page.waitForFunction(()=>{const v=document.querySelector('video');return v?.readyState>=3&&v.currentTime>13&&!v.paused;});
    assert.equal(await video.evaluate(v=>v.playbackRate),2,'Speed survives track changes');
    await video.evaluate(v=>v.pause());
    await page.getByText('MediaInfo · text report',{exact:true}).click();
    assert.match(await page.getByLabel('MediaInfo text report',{exact:true}).innerText(),/Matroska/);
    await page.screenshot({path:output+'/media-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:output+'/media-mobile.png',fullPage:true});
    assert.ok(await page.locator('.final-media').evaluate(el=>el.scrollWidth<=el.clientWidth),'Player fits mobile');
    await page.getByRole('button',{name:'Close video preview',exact:true}).click();
    assert.equal(await video.count(),0);
    assert.ok(requests.some(s=>s.includes('subtitle=5')));
    assert.ok(requests.some(s=>s.includes('video=1&audio=3')));
    assert.deepEqual(errors,[]);
    console.log('Real browser preview passed: playback, seeking, PGS selection, video/audio switching, position preservation, MediaInfo, encoder summary, source bitrate and mobile layout.');
  } finally {await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
