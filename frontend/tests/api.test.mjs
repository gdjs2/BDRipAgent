import assert from 'node:assert/strict';
import { test } from 'node:test';
import { api, ApiError } from '../src/api.ts';

test('GET recovers a dropped connection and a temporary proxy error', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => {
    calls++;
    if (calls === 1) throw new TypeError('Failed to fetch');
    if (calls === 2) return new Response('Bad Gateway', { status: 502 });
    return Response.json({ ok: true });
  });
  assert.deepEqual(await api('/jobs'), { ok: true });
  assert.equal(calls, 3);
});

test('persistent connection failure is bounded and has a readable message', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => {
    calls++; throw new TypeError('Failed to fetch');
  });
  await assert.rejects(api('/jobs'), error => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.retryable, true);
    assert.match(error.message, /Cannot reach the server/);
    return true;
  });
  assert.equal(calls, 3);
});

test('unconfirmed POST actions are never replayed automatically', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => {
    calls++; throw new TypeError('Failed to fetch');
  });
  await assert.rejects(api('/jobs', { title: 'Fixture' }), /check whether it completed/);
  assert.equal(calls, 1);
});

test('validation errors retain their message and are not retried', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => {
    calls++; return Response.json({ detail: 'Retry the latest attempt' }, { status: 409 });
  });
  await assert.rejects(api('/tasks/id/retry', {}), error => {
    assert.equal(error.status, 409);
    assert.equal(error.retryable, false);
    assert.equal(error.message, 'Retry the latest attempt');
    return true;
  });
  assert.equal(calls, 1);
});

test('only authentication failures emit session-expired', async t => {
  const originalWindow = globalThis.window;
  globalThis.window = new EventTarget();
  t.after(() => { globalThis.window = originalWindow; });
  let expired = 0, calls = 0;
  window.addEventListener('session-expired', () => expired++);
  t.mock.method(globalThis, 'fetch', async () => {
    calls++; return Response.json({ detail: 'Invalid session' }, { status: 401 });
  });
  await assert.rejects(api('/config'), /Invalid session/);
  assert.equal(expired, 1);
  await assert.rejects(api('/session', { token: 'wrong' }), /Invalid session/);
  assert.equal(expired, 1);
  assert.equal(calls, 2);
});

test('a lost response body is retried only for reads', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => {
    calls++;
    if (calls === 1) return { ok: true, status: 200, json: async () => { throw new TypeError('terminated'); } };
    return Response.json({ ok: true });
  });
  assert.deepEqual(await api('/jobs'), { ok: true });
  assert.equal(calls, 2);
});
