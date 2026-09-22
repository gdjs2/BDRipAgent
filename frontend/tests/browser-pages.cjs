// Read-only page audit against a running app. Supply a session-cookie JSON file.
const fs = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  const base = process.env.APP_BASE_URL || 'http://127.0.0.1:8080';
  const output = process.env.UI_SCREENSHOT_DIR || '/tmp/page-review';
  fs.mkdirSync(output, {recursive:true});
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  const report = [];
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors=[], mutations=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/**', route => {if(['GET','HEAD','OPTIONS'].includes(route.request().method())) return route.continue(); mutations.push(route.request().url()); return route.abort();});
    await page.goto(base);
    await page.getByRole('heading',{name:'Your encoding workspace.'}).waitFor();
    for(const [size,width,height] of [['desktop',1440,1000],['tablet',900,1000],['mobile',390,844]]) {
      await page.setViewportSize({width,height});
      const geometry=await page.evaluate(()=>({viewport:innerWidth,width:document.documentElement.scrollWidth,activeTabVisible:true}));
      await page.screenshot({path:`${output}/login-${size}.png`});
      report.push({page:'login',size,...geometry,errors:[...errors]});
    }
    if(process.env.UI_COOKIE_FILE) await context.addCookies(JSON.parse(fs.readFileSync(process.env.UI_COOKIE_FILE,'utf8')));
    else {const login=await context.request.post(base+'/api/session',{data:{token:process.env.UI_TEST_TOKEN}}); if(!login.ok()) throw Error('Test login failed');}
    const response=await context.request.get(base+'/api/jobs');
    if (!response.ok()) throw Error('Cannot read jobs: '+response.status());
    const jobs=await response.json();
    const id=process.env.UI_JOB_ID || jobs.find(j=>j.title.includes('They Will Kill You'))?.id || jobs[0]?.id;
    if(!id) throw Error('No job available for page review');
    const routes=[['dashboard','/'],['new-job','/new'],['queue','/queue'],...['','tracks','crf','encode','screenshots','release','artifacts'].map(tab=>[tab||'overview',`/jobs/${id}/${tab}`])];
    for(const [size,width,height] of [['desktop',1440,1000],['tablet',900,1000],['mobile',390,844]]) {
      await page.setViewportSize({width,height});
      for(const [name,path] of routes) {
        const errorStart=errors.length;
        await page.goto(base+path);
        await page.locator('main h1').waitFor();
        await page.waitForTimeout(900);
        const geometry=await page.evaluate(()=>({
          viewport:innerWidth, width:document.documentElement.scrollWidth,
          activeTabVisible: (()=>{const tab=document.querySelector('.tabs [aria-current=page]');if(!tab)return true;const rect=tab.getBoundingClientRect();return rect.left>=0 && rect.right<=innerWidth;})(),
          overflow:[...document.querySelectorAll('main *')].filter(el=>{
            const r=el.getBoundingClientRect();
            return r.width>0 && (r.right>innerWidth+1 || r.left < -1) && getComputedStyle(el).position!=='fixed';
          }).slice(0,12).map(el=>({tag:el.tagName,class:el.className,text:el.textContent.trim().slice(0,90)})),
          headings:[...document.querySelectorAll('main h2')].map(el=>el.textContent),
        }));
        await page.screenshot({path:`${output}/${name}-${size}.png`,fullPage:true});
        report.push({page:name,size,...geometry,errors:errors.slice(errorStart)});
        console.log(JSON.stringify({page:name,size,width:geometry.width,viewport:width,overflow:geometry.overflow.length,errors:errors.slice(errorStart)}));
      }
    }
    fs.writeFileSync(output+'/report.json',JSON.stringify(report,null,2));
    if(report.some(r=>r.width>r.viewport || r.errors.length || !r.activeTabVisible) || mutations.length) {console.error('Page checks failed', {mutations});process.exitCode=1;}
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
