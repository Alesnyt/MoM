import assert from 'node:assert/strict';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { loadSource } from './load-source.mjs';
const { startPolling } = loadSource('poll.ts');

async function until(check) {
  for (let i = 0; i < 200; i++) {
    if (check()) return;
    await delay(5);
  }
  assert.fail('Polling condition did not complete');
}

test('slow requests never overlap and polling resumes after success', async (t) => {
  const pending = [];
  const received = [];
  let calls = 0;
  const stop = startPolling(() => { calls++; return new Promise(resolve => pending.push(resolve)); },
    value => received.push(value), 5);
  t.after(stop);
  await until(() => calls === 1);
  await delay(30);
  assert.equal(calls, 1);
  pending.shift()('first');
  await until(() => calls === 2);
  assert.deepEqual(received, ['first']);
});

test('cleanup aborts in-flight requests and ignores even a transport that returns late', async () => {
  let resolveRequest, signal;
  const received = [];
  const stop = startPolling(s => { signal = s; return new Promise(r => { resolveRequest = r; }); },
    value => received.push(value), 5);
  await until(() => resolveRequest);
  stop();
  assert.equal(signal.aborted, true);
  resolveRequest('obsolete');
  await delay(30);
  assert.deepEqual(received, []);
});

test('failed requests retry sequentially without erasing the last value', async (t) => {
  let calls = 0;
  const received = [];
  const stop = startPolling(async () => {
    if (++calls === 1) throw new Error('offline');
    return 'online';
  }, value => received.push(value), 5);
  t.after(stop);
  await until(() => received.length);
  assert.equal(calls, 2);
  assert.deepEqual(received, ['online']);
});
