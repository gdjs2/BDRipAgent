// Optional browser acceptance; API/agent responses are fixtures, media conversion is tested in pytest.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');
(async () => {
  const server = spawn('npm', ['run','preview','--','--host','127.0.0.1','--port','4175','--strictPort'], {stdio:'pipe',detached:true});
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  const output = process.env.UI_SCREENSHOT_DIR || '/tmp/discovery-ui';
  mkdirSync(output,{recursive:true});
  try {
    for(let i=0;i<100;i++) {try {if((await fetch('http://127.0.0.1:4175')).ok) break;} catch {} await new Promise(r=>setTimeout(r,100));}
    const policy = {count:7,representative:4,encode_challenging:3,min_spacing_seconds:30,min_timeline_bins:3,max_per_scene:1,policy:'fixture',decoder:'cpu'};
    const flags = {default:false,forced:false,hearing_impaired:false,visual_impaired:false,commentary:false};
    const track = (id,kind,name,language,extra={}) => ({track_id:id,kind,info:{track_id:id,kind,name,base_name:name,mux_name:name,language,source_order:id,extractable:true,codec:kind==='audio'?'DTS':'PGS',codec_id:kind==='audio'?'A_DTS':'S_HDMV/PGS',...flags,track_review:{schema_version:1,description:'Dialogue checked against source samples.',confidence:'high',flag_explanation:'Sampled content reviewed.'},...(kind==='subtitles'?{subtitle_detection:{schema_version:1,language_confident:true,sdh_confident:true,hearing_impaired:false,status:'resolved'}}:{}),...extra}});
    let job = {id:'fixture',title:'The Night Train',year:2026,source_path:'The.Night.Train.mkv',state:'ENCODING',tracks_editable:true,track_analysis_complete:true,analysis_profile:'x265-live',analysis:{track_review_version:1},validation:{},tasks:[],artifacts:[],screenshot_policy:policy,tracks:[track(1,'audio','English DTS','en'),track(2,'subtitles','English PGS','en')],track_selection:{audio_track_ids:[1],subtitle_track_ids:[2]},subtitle_discovery:{allowed:true,review_required:false,policy:{enabled:false,original_languages:[]},missing:['zh-Hans','zh-Hant'],original_language_unknown:true,active_task:null,report:null}};
    const page = await browser.newPage({viewport:{width:1360,height:1050}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    let created, requested, saved;
    await page.route('**/api/**',async route=>{
      const path=new URL(route.request().url()).pathname;
      let body;
      if(path.endsWith('/events')||path.endsWith('/agent-events')) return route.fulfill({status:200,contentType:'text/event-stream',body:': fixture\n\n'});
      if(path==='/api/config') body={profiles:{'x265-live':{codec:'x265',encoder:'x265',preset:'slow',bit_depth:10}},screenshots:policy,stages:['ANALYZING_SOURCE','WAITING_FOR_TRACK_SELECTION','ENCODING']};
      else if(path==='/api/sources') body=[{path:'The.Night.Train.mkv',size:1000000000}];
      else if(path==='/api/jobs' && route.request().method()==='POST') {created=route.request().postDataJSON();body=job;}
      else if(path.endsWith('/subtitles/discover')) {
        requested=route.request().postDataJSON();
        job={...job,subtitle_discovery:{...job.subtitle_discovery,policy:requested,active_task:{id:'search',job_id:job.id,status:'RUNNING',progress:0,detail:{phase:'Matching dialogue across the source timeline'}}}};
        if(job.state==='COMPLETE') job={...job,remux:{available:false,reason:'Wait for the current task to finish.',backup_days:3},subtitle_discovery:{...job.subtitle_discovery,allowed:false}};
        body=job;
      } else if(path.endsWith('/tracks/selection')) {
        saved=route.request().postDataJSON();
        job={...job,track_selection:saved,subtitle_discovery:{...job.subtitle_discovery,review_required:false}};
        body=job;
      } else if(path==='/api/jobs/fixture/subtitles/discovered/1000000') {
        assert.equal(route.request().method(),'DELETE');
        job={...job,tracks:job.tracks.filter(t=>t.track_id!==1000000),subtitle_discovery:{...job.subtitle_discovery,report:{...job.subtitle_discovery.report,added_tracks:[],candidates:job.subtitle_discovery.report.candidates.map(c=>c.track_id===1000000?{...c,status:'removed'}:c)}}};body=job;
      }
      else if(path==='/api/jobs/fixture') body=job;
      else if(path==='/api/queue') body={running:[],queued:[],paused:false,max_encoding_tasks:1,max_other_tasks:3,max_crf_tasks:1,effective_encoding_limit:1,effective_other_limit:3,effective_crf_limit:1,running_encoding_tasks:0,running_other_tasks:0,running_crf_tasks:0};
      else body=[];
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto('http://127.0.0.1:4175/new');
    await page.getByRole('heading',{name:'New movie job'}).waitFor();
    const auto=page.getByLabel('Find missing subtitles during analysis');
    assert.equal(await auto.isChecked(),false,'Online discovery must be opt-in');
    await auto.check();
    await page.getByLabel('Original language codes (optional)').fill('ko');
    await page.getByRole('button',{name:/The.Night.Train.mkv/}).click();
    await page.getByLabel('Movie title',{exact:true}).fill('The Night Train');
    await page.getByLabel('Release year',{exact:true}).fill('2026');
    await page.getByRole('button',{name:'Create job & analyze →',exact:true}).click();
    await page.waitForURL('**/jobs/fixture');
    assert.deepEqual(created.subtitle_discovery,{enabled:true,original_languages:['ko']});
    await page.goto('http://127.0.0.1:4175/jobs/fixture/tracks');
    await page.getByRole('heading',{name:'Find missing subtitles',exact:true}).waitFor();
    await page.getByLabel('Subtitle search original languages').fill('en');
    await page.getByRole('button',{name:'Find missing subtitles',exact:true}).click();
    await page.getByText('Searching & aligning',{exact:true}).waitFor();
    assert.deepEqual(requested,{enabled:false,original_languages:['en']});
    await page.getByText(/Matching dialogue across the source timeline/).waitFor();
    assert.equal(await page.getByRole('button',{name:'Update track choices',exact:true}).isDisabled(),true,'Confirming choices must wait for discovery');
    assert.equal(await page.getByRole('button',{name:'Cancel search',exact:true}).isEnabled(),true);
    await page.screenshot({path:output+'/discovery-running-desktop.png',fullPage:true});
    const provenance={requires_attention:true,critical_errors:['CRITICAL cue 1467: damaged dialogue retained after reference checks.'],source_url:'https://example.org/subtitle/movie',download_url:'https://example.org/movie.zip',source_filename:'The.Night.Train.25fps.zh-Hans.srt',release:'Blu-ray edition',selection_reason:'The dialogue and edition match; simplified characters confirmed.',converter:'Subtitle Edit 5.2.0',crop_checked:true,fetched_at:new Date().toISOString(),review:{explanation:'Nine distinct dialogue matches span the opening, middle and ending. A uniform FPS correction and offset fit the source.',issues:[]},quality:{cues:1200,warnings:['Two cues have long lines; inspect the rendered PGS before release.']},alignment:{scale:25/24,offset_seconds:1.2,max_error_seconds:0.08,anchor_count:9,reference_track_id:2,movie_fps:'24',inferred_candidate_fps:25,anchors:[{candidate_id:1,reference_id:'2:1',explanation:'The opening train announcement matches.'}]}};
    job={...job,tracks:[...job.tracks,track(1000000,'subtitles','Simplified Chinese PGS','zh-Hans',{origin:'discovery',discovery:provenance})],subtitle_discovery:{...job.subtitle_discovery,active_task:null,removal:{allowed:true},review_required:true,original_language_unknown:false,missing:['zh-Hant'],report:{status:'complete',summary:'Simplified Chinese aligned. Traditional Chinese needs a different edition.',original_languages:['en'],original_language_sources:['https://example.org/movie'],source_job_id:'fixture',needs_review:true,missing:['zh-Hant'],added_tracks:[provenance],candidates:[{language:'zh-Hans',source_url:provenance.source_url,reason:provenance.selection_reason,status:'added',issues:[],track_id:1000000},{language:'zh-Hant',source_url:'https://example.org/alternate',reason:'Candidate has a different cut.',status:'needs_review',issues:['Timing residuals differ across the movie.']} ]}}};
    await page.reload();
    await page.getByText(/Subtitle tracks have changed/).waitFor();
    assert.equal(await page.locator('#include-track-1000000').isChecked(),false,'Discovered subtitles remain unselected');
    await page.getByText('Agent-found PGS · Aligned & checked',{exact:true}).waitFor();
    await page.locator('[aria-controls="track-details-1000000"]').click();
    await page.getByRole('heading',{name:'Agent-found subtitle',exact:true}).waitFor();
    await page.getByText('× 1.041667',{exact:true}).waitFor();
    await page.getByText('+1.200 s',{exact:true}).waitFor();
    await page.getByText('80 ms',{exact:true}).waitFor();
    assert.equal(await page.getByRole('link',{name:'Source page',exact:true}).getAttribute('href'),provenance.source_url);
    await page.getByText('Matched dialogue and alignment reasoning',{exact:true}).click();
    await page.getByText(/The opening train announcement matches/).waitFor();
    await page.screenshot({path:output+'/discovery-review-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:output+'/discovery-review-mobile.png',fullPage:true});
    assert.equal(await page.locator('.track-selection').evaluate(el=>el.scrollWidth<=el.clientWidth),true,'Track layout must fit mobile');
    assert.equal(await page.locator('.subtitle-discovery-panel').evaluate(el=>el.scrollWidth<=el.clientWidth),true,'Discovery controls must fit mobile');
    await page.locator('#include-track-1000000').check();
    await page.getByRole('button',{name:'Update track choices',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.track-confirm button').disabled);
    await page.getByText('PGS ready · Critical issues in report',{exact:true}).waitFor();
    await page.getByText('PGS generated with critical subtitle issues',{exact:true}).first().waitFor();
    assert.deepEqual(saved.subtitle_track_ids,[2,1000000]);
    await page.getByText(/Subtitle tracks have changed/).waitFor({state:'hidden'});
    await page.getByRole('button',{name:'Remove agent-found track #1000000',exact:true}).click();
    await page.getByText('Subtitle track removed from this source. Remux existing videos to update their tracks.',{exact:true}).waitFor();
    assert.equal(await page.locator('#include-track-1000000').count(),0);
    await page.getByText('Removed from this source',{exact:true}).waitFor();
    job={...job,state:'COMPLETE',tracks_editable:false,remux:{available:true,backup_days:3}};
    await page.setViewportSize({width:1360,height:1050});
    await page.reload();
    await page.getByRole('button',{name:'Edit tracks & remux',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'Search again',exact:true}).isEnabled(),true,'A completed mux must allow another subtitle search');
    await page.getByRole('button',{name:'Search again',exact:true}).click();
    await page.getByText('Searching & aligning',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Search again',exact:true}).isDisabled(),true);
    assert.equal(await page.getByRole('button',{name:'Save choices & remux',exact:true}).isDisabled(),true,'Remux must wait for discovery to finish');
    job={...job,remux:{available:true,backup_days:3},subtitle_discovery:{...job.subtitle_discovery,active_task:null,allowed:true}};
    await page.getByRole('button',{name:'Save choices & remux',exact:true}).waitFor();
    await page.waitForFunction(()=>Array.from(document.querySelectorAll('button')).some(b=>b.textContent==='Save choices & remux'&&!b.disabled));
    assert.equal(await page.getByRole('button',{name:'Search again',exact:true}).isEnabled(),true);
    assert.deepEqual(errors,[]);
    console.log('Subtitle discovery browser acceptance passed: opt-in, manual search, confirmation gate, provenance, mobile layout.');
  } finally {await browser.close();try{process.kill(-server.pid,'SIGTERM');}catch{}}
})().catch(error=>{console.error(error);process.exitCode=1;});
