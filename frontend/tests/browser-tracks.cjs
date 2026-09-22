// Optional browser regression: NODE_PATH must include Playwright. Uses only mocked APIs.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const server = spawn('npm', ['run', 'preview', '--', '--host', '127.0.0.1', '--port', '4175', '--strictPort'], {stdio: 'pipe', detached: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const output = process.env.UI_SCREENSHOT_DIR || '/tmp/track-ui';
  mkdirSync(output, {recursive: true});
  try {
    for (let i = 0; i < 100; i++) {
      try {if ((await fetch('http://127.0.0.1:4175')).ok) break;} catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const flags = {default: false, forced: false, hearing_impaired: false, visual_impaired: false, commentary: false};
    const makeTrack = (id, kind, name, language, description, extra={}) => ({track_id: id, kind, info: {source_order:id, name, base_name: name, mux_name: name, language, codec: kind === 'audio' ? 'DTS-HD MA' : 'PGS', codec_id: kind === 'audio' ? 'A_DTS' : 'S_HDMV/PGS', extractable: true, ...flags, track_review: {schema_version: 1, description, confidence: 'high', flag_explanation: 'Findings are based on sampled content. Review the flags before confirming.'}, ...(kind === 'subtitles' ? {subtitle_detection: {schema_version: 1, language_confident: true, status: 'resolved', hearing_impaired: false, explanation: description, sampled_cues: 96, unique_cues: 1280, method: 'program + agent'}} : {}), ...extra}});
    let job = {id: 'fixture', title: 'The Night Train', source_path: 'The.Night.Train.2026.BluRay.mkv', state: 'ENCODING', tracks_editable: true, track_analysis_complete: true, analysis_profile: 'x265-live', analysis: {track_review_version: 1}, validation: {}, tasks: [], artifacts: [], screenshot_policy: {}, tracks: [
      makeTrack(1, 'audio', 'English DTS-MA 5.1', 'en', 'English main soundtrack. Sampled dialogue matches the film; no commentary or audio description was identified.', {default: true, channels: 6, sample_rate: 48000}),
      makeTrack(2, 'audio', 'French DTS-MA 5.1', 'fr', 'French dialogue in the sampled passages. Likely an alternate-language soundtrack; the sample cannot establish every scene.'),
      makeTrack(3, 'subtitles', 'English PGS', 'en', 'English dialogue with sound effects and speaker labels. Accessibility features support an SDH flag.', {hearing_impaired: true}),
      makeTrack(4, 'subtitles', 'French PGS', 'fr', 'French dialogue across the film. SDH remains uncertain in these samples.', {hearing_impaired: null}),
      makeTrack(5, 'subtitles', 'Traditional Chinese PGS', 'zh-Hant', 'Traditional Chinese written dialogue. No consistent sound descriptions were found in the reviewed cues.'),
    ]};
    job.tracks.reverse(); // Database arrival order must not determine the initial display.
    job.analysis.audio_comparison = {summary: 'Track 1 has English dialogue; track 2 has French dialogue. Both are 5.1 soundtracks.', status: 'resolved', rounds: 2, max_rounds: 6, distinctions: [{track_id: 1, compared_with: [2], difference: 'English main dialogue, compared with the French dub on track 2.', resolved: true, evidence_sample_ids: [1]}, {track_id: 2, compared_with: [1], difference: 'French dialogue at the same sampled times as track 1.', resolved: true, evidence_sample_ids: [1]}]};
    const page = await browser.newPage({viewport: {width: 1360, height: 1050}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let analyses = 0, selection, releaseDraft;
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      let body;
      if (path.endsWith('/events') || path.endsWith('/agent-events')) return route.fulfill({status: 200, contentType: 'text/event-stream', body: ': fixture\n\n'});
      if (path === '/api/config') body = {profiles: {'x265-live': {codec: 'x265'}}, screenshots: {}, stages: ['ANALYZING_SOURCE', 'WAITING_FOR_TRACK_SELECTION']};
      else if (path === '/api/queue') body = {running: [], queued: [], paused: false, max_encoding_tasks: 1, max_other_tasks: 3, effective_encoding_limit: 1, effective_other_limit: 3, running_encoding_tasks: 0, running_other_tasks: 0};
      else if (path === '/api/languages/verify') body={code:'en',language_name:'English'};
      else if (path.endsWith('/tracks/upload')) {
        assert.match(route.request().postData(),/Hello world/);
        assert.equal(new URL(route.request().url()).searchParams.get('hearing_impaired'),'false');
        job={...job,subtitle_uploads:[{id:'upload1',task_id:'review1',filename:'English.srt',language:'en',status:'QUEUED',detail:{phase:'Waiting for subtitle review'}}],subtitle_discovery:{...job.subtitle_discovery,allowed:false,policy:{enabled:false,original_languages:['en']},missing:[],active_task:{id:'review1',job_id:job.id,type:'review_uploaded_subtitle',status:'QUEUED',detail:{phase:'Waiting for subtitle review'}}}};
        body={duplicate:false,queued:true,task_id:'review1',upload_id:'upload1'};
      }
      else if (path.endsWith('/tracks/analyze')) {analyses++; job = {...job, state: 'ANALYZING_SOURCE', tracks_editable: false}; body = job;}
      else if (path.endsWith('/language')) {
        const code=new URL(route.request().url()).searchParams.get('code');
        if(code==='jpn') body={code:'ja',language_name:'Japanese',track_name:'Japanese PGS',base_name:'Japanese PGS'};
        else return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'Invalid or undetermined language code'})});
      }
      else if (path.endsWith('/tracks/selection') || path.endsWith('/remux')) {
        selection = route.request().postDataJSON();
        const revision = (job.shared_track_selection?.revision ?? 0) + 1;
        job = {...job, shared_track_selection: {revision, applied_revision: revision, source_job_id:job.id, pending:false}, track_selection: {audio_track_ids: selection.audio_track_ids, subtitle_track_ids: selection.subtitle_track_ids}, tracks: job.tracks.map(track => ({...track, info: {...track.info, ...(selection.track_flags[track.track_id] || {}), ...(selection.track_names[track.track_id] ? {name_override:selection.track_names[track.track_id]} : {}), ...(selection.track_languages[track.track_id] ? {language:selection.track_languages[track.track_id],language_override:selection.track_languages[track.track_id],language_name:'Japanese'} : {})}}))};
        if(path.endsWith('/remux')) job={...job,state:'PREPARING_TRACKS',tracks_editable:false,remux:{...job.remux,available:false}};
        body = job;
      }
      else if (path.endsWith('/release')) {releaseDraft = route.request().postDataJSON(); body = {...job, analysis: {...job.analysis, release_details: releaseDraft}};}
      else if (path === '/api/jobs/fixture') body = job;
      else body = [];
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
    });
    await page.goto('http://127.0.0.1:4175/jobs/fixture/tracks');
    await page.getByRole('heading', {name: 'Choose your tracks'}).waitFor();
    assert.equal(await page.locator('.track-row').count(), 5);
    assert.deepEqual(await page.locator('.track-list[data-track-kind="audio"] .track-row').evaluateAll(rows=>rows.map(row=>Number(row.dataset.trackId))), [1,2]);
    assert.deepEqual(await page.locator('.track-list[data-track-kind="subtitles"] .track-row').evaluateAll(rows=>rows.map(row=>Number(row.dataset.trackId))), [3,4,5]);
    await page.getByRole('heading', {name: 'Audio differences'}).waitFor();
    assert.equal(await page.locator('.track-details').count(), 0);
    assert.equal(analyses, 0, 'Completed legacy-version reviews must not be restarted');
    const heights = await page.locator('.track-summary').evaluateAll(rows => rows.map(row => row.getBoundingClientRect().height));
    assert.ok(Math.max(...heights) < 150, `Collapsed rows are too tall: ${heights}`);
    await page.screenshot({path: output+'/tracks-desktop.png', fullPage: true});
    await page.locator('#include-track-1').check();
    await page.locator('#include-track-4').check();
    await page.locator('#track-details-4').waitFor();
    assert.equal(await page.getByRole('button', {name: 'Confirm tracks →'}).isDisabled(), true);
    await page.getByLabel('SDH for track 4').selectOption('false');
    await page.locator('#track-name-4').fill('French — Custom name');
    await page.getByRole('button', {name: 'Confirm tracks →'}).click();
    await page.waitForFunction(() => !document.querySelector('.track-confirm button').disabled);
    assert.deepEqual(selection.audio_track_ids, [1]);
    assert.deepEqual(selection.subtitle_track_ids, [4]);
    assert.equal(selection.track_flags['4'].hearing_impaired, false);
    assert.equal(selection.track_names['4'], 'French — Custom name');
    // Reorder with real mouse input; order is independent from checkbox click order.
    await page.locator('#include-track-2').check();
    await page.locator('#include-track-3').check();
    await page.locator('#include-track-5').check();
    await page.locator('[aria-controls="track-details-4"]').click();
    const ids = kind => page.locator(`.track-list[data-track-kind="${kind}"] .track-row`).evaluateAll(rows=>rows.map(row=>Number(row.dataset.trackId)));
    async function drag(from,to,after=false,cancel=false) {
      const handle=page.locator(`[data-track-id="${from}"] .track-drag-handle`);
      await handle.scrollIntoViewIfNeeded();
      const start=await handle.boundingBox();
      await page.mouse.move(start.x+start.width/2,start.y+start.height/2);
      await page.mouse.down();
      const target=page.locator(`[data-track-id="${to}"]`);
      await target.evaluate(el=>el.scrollIntoView({block:'center'}));
      const end=await target.boundingBox();
      await page.mouse.move(end.x+end.width/2,end.y+(after?end.height-8:8),{steps:8});
      if(cancel) await page.keyboard.press('Escape');
      await page.mouse.up();
    }
    await drag(2,1);
    assert.deepEqual(await ids('audio'),[2,1]);
    await drag(5,3);
    assert.deepEqual(await ids('subtitles'),[5,3,4]);
    await page.getByRole('button',{name:'Reorder subtitle track 3',exact:true}).press('End');
    assert.deepEqual(await ids('subtitles'),[5,4,3]);
    await drag(5,3,false,true);
    assert.deepEqual(await ids('subtitles'),[5,4,3], 'Escape must cancel a drag');
    await drag(1,5);
    assert.deepEqual(await ids('audio'),[2,1], 'Cross-group drops must not change audio order');
    assert.deepEqual(await ids('subtitles'),[5,4,3], 'Cross-group drops must not change subtitle order');
    await page.locator('#include-track-2').uncheck();
    await page.locator('#include-track-2').check();
    assert.deepEqual(await ids('audio'),[2,1]);
    await page.getByText('ORDER NOT SAVED',{exact:true}).waitFor();
    await page.waitForTimeout(5200);
    assert.deepEqual(await ids('audio'),[2,1], 'Polling must not discard unsaved order');
    await page.getByRole('button',{name:'Update track choices',exact:true}).click();
    await page.getByText('CONFIRMED',{exact:true}).waitFor();
    assert.deepEqual(selection.audio_track_ids,[2,1]);
    assert.deepEqual(selection.subtitle_track_ids,[5,4,3]);
    assert.equal(selection.track_flags['4'].hearing_impaired,false);
    assert.equal(selection.track_names['4'],'French — Custom name');
    await page.reload();
    await page.getByRole('heading',{name:'Choose your tracks'}).waitFor();
    assert.deepEqual(await ids('audio'),[2,1]);
    assert.deepEqual(await ids('subtitles'),[5,4,3]);
    await page.screenshot({path:output+'/tracks-reordered.png',fullPage:true});
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: output+'/tracks-mobile-expanded.png', fullPage: true});
    assert.equal(await page.locator('.track-selection').evaluate(el => el.scrollWidth <= el.clientWidth), true, 'Track controls overflow on mobile');
    assert.deepEqual(errors, []);
    // Chromium touch events exercise pointer capture on a narrow viewport.
    const cdp=await page.context().newCDPSession(page);
    await cdp.send('Emulation.setTouchEmulationEnabled',{enabled:true});
    await page.locator('.track-list[data-track-kind="audio"]').scrollIntoViewIfNeeded();
    const touchStart=await page.getByRole('button',{name:'Reorder audio track 2',exact:true}).boundingBox();
    const touchEnd=await page.locator('[data-track-id="1"]').boundingBox();
    const x=touchStart.x+touchStart.width/2, y=touchStart.y+touchStart.height/2;
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
    for(let step=1;step<=8;step++) await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:touchEnd.x+touchEnd.width/2,y:y+(touchEnd.y+touchEnd.height-8-y)*step/8}]});
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    await page.waitForFunction(()=>document.querySelector('.track-list[data-track-kind="audio"] .track-row')?.dataset.trackId==='1');
    assert.deepEqual(await ids('audio'),[1,2], 'Touch dragging must reorder audio');
    await page.getByRole('button',{name:'Update track choices',exact:true}).click();
    await page.getByText('CONFIRMED',{exact:true}).waitFor();
    assert.deepEqual(selection.audio_track_ids,[1,2]);
    await cdp.detach();
    await page.screenshot({path:output+'/tracks-reordered-touch.png',fullPage:true});
    // A sibling's confirmed choices appear without reloading an untouched form.
    function siblingChoices(audio, subtitles, name) {
      const revision=job.shared_track_selection.revision+1;
      job={...job, shared_track_selection:{revision,applied_revision:revision,source_job_id:'sibling',pending:false},track_selection:{audio_track_ids:audio,subtitle_track_ids:subtitles},tracks:job.tracks.map(t=>t.track_id===4?{...t,info:{...t.info,name_override:name,forced:true}}:t)};
    }
    siblingChoices([2,1],[4,5,3],'Shared subtitle name');
    await page.waitForTimeout(5500);
    assert.deepEqual(await ids('audio'),[2,1]);
    assert.deepEqual(await ids('subtitles'),[4,5,3]);
    if (!(await page.locator('#track-name-4').count())) await page.locator('[aria-controls="track-details-4"]').click();
    assert.equal(await page.locator('#track-name-4').inputValue(),'Shared subtitle name');
    assert.equal(await page.getByLabel('Forced for track 4').inputValue(),'true');
    // Remote edits must not discard a local name or drag order, nor silently overwrite a sibling.
    await page.locator('#track-name-4').fill('My unsaved name');
    await page.getByRole('button',{name:'Reorder audio track 1',exact:true}).press('Home');
    siblingChoices([2],[5,4],'New shared subtitle name');
    await page.getByRole('button',{name:'Load shared choices',exact:true}).waitFor();
    assert.equal(await page.locator('#track-name-4').inputValue(),'My unsaved name');
    assert.deepEqual(await ids('audio'),[1,2]);
    assert.equal(await page.getByRole('button',{name:'Update track choices',exact:true}).isDisabled(),true);
    await page.getByRole('button',{name:'Load shared choices',exact:true}).click();
    assert.equal(await page.locator('#include-track-1').isChecked(),false);
    assert.deepEqual(await ids('audio'),[2,1]);
    assert.equal(await page.locator('#track-name-4').inputValue(),'New shared subtitle name');
    await page.getByRole('button',{name:'Update track choices',exact:true}).click();
    await page.getByText('CONFIRMED',{exact:true}).waitFor();
    assert.equal(selection.shared_revision,job.shared_track_selection.revision-1);
    await page.screenshot({path:output+'/tracks-shared-choices.png',fullPage:true});
    job.analysis.audio_comparison = {...job.analysis.audio_comparison, status: 'inconclusive', requires_human: true, question: 'Check whether track 2 is an alternate dub.', stop_reason: 'The configured limit of 2 comparison rounds was reached.'};
    await page.reload();
    await page.getByText('Manual review needed', {exact: true}).waitFor();
    await page.getByText('Needs your review: Check whether track 2 is an alternate dub.', {exact: true}).waitFor();
    assert.equal(await page.locator('#include-track-1').isDisabled(), false, 'Budget exhaustion must hand control to the user');
    const description='[b]电影简介[/b]\nA shared synopsis.\n\nAnother paragraph.';
    job.analysis.release_details={chinese_name:'共享片名',source:'1080p Blu-ray AVC',tracker:'https://example.com/announce',extra_description:'双语',movie_description:description,upload_screenshots:false};
    job.analysis.shared_release_details={source_job_id:'sibling',title:'The Night Train x264'};
    await page.goto('http://127.0.0.1:4175/jobs/fixture/release');
    await page.getByText(/Release information is saved for this source video/).waitFor();
    assert.equal(await page.getByLabel('Chinese name', {exact:true}).inputValue(),'共享片名');
    assert.equal(await page.getByLabel('Movie description (BBCode, optional)').inputValue(),description);
    await page.getByLabel('Movie description (BBCode, optional)').fill(description+'\nMy edit.');
    job.analysis.release_details={...job.analysis.release_details,movie_description:'Updated defaults from sibling'};
    await page.waitForTimeout(5500); // Polling must preserve unsaved input.
    assert.equal(await page.getByLabel('Movie description (BBCode, optional)').inputValue(),description+'\nMy edit.');
    await page.getByLabel('Chinese name', {exact: true}).fill('夜车');
    await page.getByLabel('Source', {exact: true}).fill('1080p Blu-ray AVC');
    await page.getByLabel('Tracker announce URL').fill('https://example.com/announce');
    assert.equal(await page.getByRole('button', {name: 'Generate files', exact: true}).isDisabled(), true);
    assert.equal(await page.getByLabel('Upload screenshots to TTG').isDisabled(), true);
    await page.getByRole('button', {name: 'Save details', exact: true}).click();
    await page.getByText('Release details saved.', {exact: true}).waitFor();
    assert.equal(releaseDraft.chinese_name, '夜车');
    assert.equal(releaseDraft.movie_description, description+'\nMy edit.');
    await page.screenshot({path: output+'/early-release-mobile.png', fullPage: true});
    // Completed jobs can revise track metadata and remux without requesting an encode.
    job={...job,state:'COMPLETE',tracks_editable:false,remux:{available:true,backup_days:3}};
    await page.goto('http://127.0.0.1:4175/jobs/fixture/tracks');
    assert.equal(await page.locator('#include-track-4').isDisabled(),true);
    await page.getByRole('button',{name:'Edit tracks & remux',exact:true}).click();
    await page.locator('[aria-controls="track-details-4"]').click();
    await page.locator('#track-language-4').fill('bad-code');
    await page.locator('#track-language-4').blur();
    await page.getByText(/Invalid or undetermined language code/).waitFor();
    assert.equal(await page.getByRole('button',{name:'Save choices & remux',exact:true}).isDisabled(),true);
    await page.locator('#track-language-4').fill('jpn');
    await page.locator('#track-language-4').blur();
    await page.getByText('Verified: ja · Japanese',{exact:true}).waitFor();
    await page.locator('[data-track-id="4"]').getByRole('button',{name:'Use suggested name',exact:true}).click();
    assert.equal(await page.locator('#track-name-4').inputValue(),'Japanese PGS Forced');
    // Upload into a completed job while preserving unsaved language/name changes.
    await page.getByText('Add subtitles from a file',{exact:false}).click();
    await page.locator('.subtitle-upload input[type="file"]').setInputFiles({name:'English.srt',mimeType:'application/x-subrip',buffer:Buffer.from('1\n00:00:00,100 --> 00:00:01,100\nHello world\n')});
    await page.locator('.subtitle-upload input:not([type="file"])').fill('eng');
    await page.getByText('English (en)',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Upload subtitle',exact:true}).isEnabled(),true,'The agent determines SDH for text uploads');
    await page.getByRole('button',{name:'Upload subtitle',exact:true}).click();
    await page.getByText('Upload queued for review, alignment and PGS conversion.',{exact:true}).waitFor();
    assert.equal(await page.locator('#include-track-1000000').count(),0,'Unreviewed text must stay out of track choices');
    assert.equal(await page.getByRole('button',{name:'Save choices & remux',exact:true}).isDisabled(),true);
    assert.equal(await page.getByRole('link',{name:'Download uploaded file',exact:true}).getAttribute('href'),'/api/jobs/fixture/subtitles/uploads/upload1/download');
    const track=makeTrack(1000000,'subtitles','English PGS','en','Agent-reviewed, aligned and cropped uploaded subtitles.',{origin:'upload',original_filename:'English.srt',codec:'PGS',codec_id:'S_HDMV/PGS',hearing_impaired:true,language_name:'English'});
    job={...job,tracks:[...job.tracks,track],subtitle_uploads:job.subtitle_uploads.map(i=>({...i,status:'SUCCEEDED',detail:{phase:'Uploaded subtitle reviewed, aligned and converted to cropped PGS'}})),subtitle_discovery:{...job.subtitle_discovery,allowed:true,active_task:null}};
    await page.getByText('Uploaded · English.srt',{exact:true}).waitFor();
    await page.locator('[aria-controls="track-details-1000000"]').click();
    assert.equal(await page.locator('#track-details-1000000').getByRole('link',{name:'Download track',exact:true}).getAttribute('href'),'/api/jobs/fixture/tracks/1000000/download');
    assert.equal(await page.locator('#track-name-4').inputValue(),'Japanese PGS Forced');
    await page.locator('#include-track-1000000').check();
    await page.getByRole('button',{name:'Reorder subtitle track 1000000',exact:true}).press('Home');
    await page.screenshot({path:output+'/tracks-upload-mobile.png',fullPage:true});
    assert.equal(await page.locator('.track-selection').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    await page.setViewportSize({width:1360,height:1050});
    await page.screenshot({path:output+'/tracks-upload-desktop.png',fullPage:true});
    await page.screenshot({path:output+'/tracks-remux-language-mobile.png',fullPage:true});
    assert.equal(await page.locator('.track-selection').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    await page.getByRole('button',{name:'Save choices & remux',exact:true}).click();
    await page.waitForFunction(()=>!document.body.textContent.includes('Save choices & remux'));
    assert.equal(selection.subtitle_track_ids[0],1000000);
    assert.equal(selection.track_flags['1000000'].hearing_impaired,true);
    assert.equal(selection.track_languages['4'],'ja');
    assert.equal(selection.track_names['4'],'Japanese PGS Forced');
    assert.equal(analyses,0);
    job={...job,state:'PREPARING_TRACKS',tracks_editable:false};
    await page.goto('http://127.0.0.1:4175/jobs/fixture/tracks');
    await page.getByRole('heading',{name:'Choose your tracks'}).waitFor();
    assert.equal(await page.locator('.track-drag-handle:enabled').count(),0, 'Reordering locks when preparation starts');
    job = {...job, state:'ENCODING', tracks_editable:true, track_selection:null, track_analysis_complete: false, analysis: {}, tasks: [{id: 'failed-review', type: 'review_tracks', status: 'FAILED', stage: 'ANALYZING_TRACKS', attempt: 1, progress: 99, progress_detail: {}, created_at: '2026-09-21T03:00:00Z', error_message: 'Test review failure'}]};
    await page.goto('http://127.0.0.1:4175/jobs/fixture/tracks');
    await page.getByRole('button', {name: 'Retry track review'}).waitFor();
    await page.reload();
    await page.getByRole('button', {name: 'Retry track review'}).waitFor();
    assert.equal(analyses, 0, 'Failed reviews must not restart just from navigation or reload');
    assert.equal(await page.getByText('REVIEW STOPPED', {exact: true}).count(), 1);
    job = {...job, state: 'WAITING_FOR_TRACK_SELECTION', tracks_editable: true, track_analysis_complete: false, analysis: {}, tracks: [], tasks: []};
    const autoStarted = page.waitForResponse(response => response.url().endsWith('/tracks/analyze'));
    await page.goto('http://127.0.0.1:4175/jobs/fixture/tracks');
    await autoStarted;
    await page.reload();
    await page.waitForFunction(() => document.body.textContent.includes('Track analysis is paused or stopped'));
    assert.equal(analyses, 1, 'An unreviewed waiting job should start automatically once');
    console.log('Browser checks passed: mouse/touch/keyboard reordering, saved order, group boundaries, locking, compact rows, comparative audio, editing during encoding, early release drafts, flags, mobile layout and automatic analysis.');
  } finally {
    await browser.close();
    try { process.kill(-server.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
