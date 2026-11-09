import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/admin' });
Object.assign(globalThis, {
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
});
const React = await import('react');
const { cleanup, fireEvent, render, within } = await import('@testing-library/react');
const { default: App } = loadSource('App.tsx');
const originalFetch = globalThis.fetch;
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; });

test('admin opens on the live monitoring tab and polling stops when leaving', async () => {
  const health = { ffmpeg: true, openai: { configured: false } };
  let monitoringSignal;
  let requests = 0;
  globalThis.fetch = async (url, options) => {
    if (url === '/api/auth/status') return Response.json({ configured: true, authenticated: true });
    if (url === '/api/session') return Response.json({ authenticated: false });
    if (url === '/api/health' || url === '/api/settings') return Response.json(health);
    assert.equal(url, '/api/admin/monitoring');
    monitoringSignal = options.signal;
    if (++requests > 1) return new Response('', { status: 503 });
    return Response.json({
      sampled_at: '2026-11-09T13:00:00Z',
      queue: { active: 1, limit: 4, waiting: 2, review: 3, jobs: [
        { id: 'abcd0123-more', status: 'transcribing', progress: 67 },
      ] },
      resources: { cpu_percent: 145, rss_bytes: 1024 ** 3, process_count: 2 },
      disk: { ok: true, free_bytes: 1024 ** 3, total_bytes: 10 * 1024 ** 3, recordings_bytes: 0, min_free_bytes: 0 },
    });
  };
  const view = render(React.createElement(App));
  assert.ok(await view.findByRole('heading', { name: 'Мониторинг' }));
  assert.ok(await view.findByText('1 из 4'));
  assert.ok(view.getByText('145%'));
  const jobs = view.getByRole('table', { name: 'Активные задачи' });
  assert.ok(within(jobs).getByText('Расшифровка'));
  assert.equal(within(jobs).getByRole('progressbar', { name: 'Прогресс задачи abcd0123' }).value, 67);

  fireEvent.click(view.getByRole('button', { name: 'LLM' }));
  assert.equal(monitoringSignal.aborted, true);
  assert.equal(requests, 1);
  fireEvent.click(view.getByRole('button', { name: 'Мониторинг' }));
  assert.ok(await view.findByText(/Не удалось обновить мониторинг/));
  assert.equal(view.queryByRole('table'), null);
});
