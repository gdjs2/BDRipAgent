// Optional Playwright regression; every API request uses a disposable fixture.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const server = spawn('npm', ['run', 'preview', '--', '--host', '127.0.0.1', '--port', '4176', '--strictPort'], {stdio: 'pipe', detached: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const output = process.env.UI_SCREENSHOT_DIR || '/tmp/queue-ui';
  mkdirSync(output, {recursive: true});
  try {
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch('http://127.0.0.1:4176')).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    let queue = {max_encoding_tasks: 1, max_crf_tasks: 1, effective_crf_limit: 1, running_crf_tasks: 1, max_other_tasks: 3, max_release_tasks: 1, running_release_tasks: 1, effective_encoding_limit: 1, effective_other_limit: 3, running_encoding_tasks: 1, running_other_tasks: 2, capacity: 8, paused: false, running: [], queued: []};
    const updates = [], errors = [];
    const page = await browser.newPage({viewport: {width: 1280, height: 960}});
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      let body = [];
      if (path === '/api/config') body = {profiles: {}, screenshots: {}, stages: []};
      if (path === '/api/queue') {
        if (route.request().method() === 'PATCH') {
          const change = route.request().postDataJSON();
          updates.push(change);
          queue = {...queue, ...change};
          queue.effective_encoding_limit = queue.max_encoding_tasks;
          queue.effective_crf_limit = queue.max_crf_tasks;
          queue.effective_other_limit = queue.max_other_tasks;
        }
        body = queue;
      }
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
    });
    await page.goto('http://127.0.0.1:4176/queue');
    const encoding = page.getByLabel('Maximum encoding tasks', {exact: true});
    const crf = page.getByLabel('Maximum CRF analysis tasks', {exact: true});
    const other = page.getByLabel('Maximum other tasks', {exact: true});
    await encoding.waitFor();
    assert.equal(await encoding.inputValue(), '1');
    assert.equal(await other.inputValue(), '3');
    assert.equal(await crf.inputValue(), '1');
    await page.getByText('Encoding 1/1 · CRF 1/1 · Other tasks 2/3 (release 1/1)', {exact: true}).waitFor();
    await encoding.fill('2');
    await other.fill('4');
    await crf.fill('3');
    await page.getByRole('button', {name: 'Apply limits'}).click();
    await page.getByText('Encoding 1/2 · CRF 1/3 · Other tasks 2/4 (release 1/1)', {exact: true}).waitFor();
    assert.deepEqual(updates[0], {max_encoding_tasks: 2, max_crf_tasks: 3, max_other_tasks: 4});
    await page.reload();
    await encoding.waitFor();
    assert.equal(await encoding.inputValue(), '2');
    assert.equal(await other.inputValue(), '4');
    assert.equal(await crf.inputValue(), '3');
    await page.getByRole('button', {name: 'Pause queue'}).click();
    await page.getByRole('button', {name: 'Resume queue'}).waitFor();
    assert.deepEqual(updates[1], {paused: true});
    assert.equal(queue.max_encoding_tasks, 2);
    assert.equal(queue.max_other_tasks, 4);
    assert.equal(queue.max_crf_tasks, 3);
    await page.screenshot({path: output + '/queue-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: output + '/queue-mobile.png', fullPage: true});
    assert.equal(await page.locator('main').evaluate(el => el.scrollWidth <= el.clientWidth), true, 'Queue controls overflow on mobile');
    assert.deepEqual(errors, []);
    queue = {max_concurrent_jobs: 2, effective_limit: 2, capacity: 8, paused: false, running: queue.running, queued: []};
    await page.reload();
    await page.getByText(/current server uses a shared limit of 2 running jobs/).waitFor();
    assert.equal(await page.getByLabel('Maximum encoding tasks', {exact: true}).count(), 0);
    assert.equal(await page.getByRole('button', {name: 'Pause queue'}).isEnabled(), true);
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: separate limits, pool usage, save/reload, queue pause, mobile layout, and compatibility during a server update.');
  } finally {
    await browser.close();
    try { process.kill(-server.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
