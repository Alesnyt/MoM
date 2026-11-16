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
const { act, cleanup, fireEvent, render, waitFor, within } = await import('@testing-library/react');
const { MonitoringPanel } = loadSource('monitoring.tsx');
const { downloadDiagnostics } = loadSource('api.ts');
const { onSessionExpired } = loadSource('http.ts');
const originalFetch = globalThis.fetch;
const originalCreateURL = URL.createObjectURL;
const originalRevokeURL = URL.revokeObjectURL;
const originalClick = window.HTMLAnchorElement.prototype.click;
afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
  URL.createObjectURL = originalCreateURL;
  URL.revokeObjectURL = originalRevokeURL;
  window.HTMLAnchorElement.prototype.click = originalClick;
});

const live = {
  sampled_at: '2026-11-16T10:00:00Z', queue: { active: 0, limit: 2, waiting: 0, review: 0, jobs: [] },
  resources: { cpu_percent: 0, rss_bytes: 1024, process_count: 1 },
  disk: { ok: true, free_bytes: 1024, total_bytes: 4096, recordings_bytes: 0, min_free_bytes: 0 },
};
const summary = {
  available: true, days: 7, retention_days: 14, runs: 12, completed: 9, interrupted: 1, errors: 1, active: 1,
  stages: { asr: { samples: 5, median_seconds: 12, p95_seconds: 19 } },
  recent_runs: [{ run: 'run-1', meeting: 'meeting-1', phase: 'audio', status: 'review',
    started_at: '2026-11-16T10:00:00Z', elapsed_seconds: 70, audio_seconds: 600,
    asr_rtf: 0.1, llm_calls: 0, peak_rss_bytes: 1024 ** 3 }],
};

function mockApi(handler) {
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/monitoring') return Response.json(live);
    return handler(url, options);
  };
}

test('diagnostics shows persistent run summaries and exports the selected period', async () => {
  const calls = [];
  let downloaded;
  mockApi((url, options) => {
    calls.push(url);
    assert.equal(options.credentials, 'include');
    assert.equal(options.cache, 'no-store');
    if (url === '/api/admin/diagnostics/export?days=14') return Response.json({ schema_version: 1, runs: [] });
    assert.match(url, /^\/api\/admin\/diagnostics\?days=(7|14)$/);
    return Response.json(summary);
  });
  URL.createObjectURL = () => 'blob:diagnostics';
  URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function () { downloaded = this.download; };
  const view = render(React.createElement(MonitoringPanel));
  const section = within(view.getByRole('region', { name: 'Диагностика производительности' }));
  await section.findByText('12');
  assert.match(section.getByText('С ошибкой').parentElement.textContent, /С ошибкой1/);
  assert.ok(section.getByText(/Тексты встреч, имена, адреса серверов и ключи не включаются/));
  assert.ok(section.getByText(/ожидание ручной разметки в их время не входит/));
  fireEvent.click(section.getByText('Время по этапам за выбранный период'));
  assert.ok(section.getByRole('table', { name: 'Сохранённые замеры этапов' }));
  assert.ok(section.getByText('19 с'));
  fireEvent.click(section.getByText('Последние запуски'));
  assert.ok(section.getByText('meeting-1'));
  assert.ok(section.getByText('Готово к разметке'));
  fireEvent.change(section.getByRole('combobox', { name: 'Период отчёта' }), { target: { value: '14' } });
  await waitFor(() => assert.ok(calls.includes('/api/admin/diagnostics?days=14')));
  await waitFor(() => assert.equal(section.getByRole('button', { name: 'Скачать отчёт' }).disabled, false));
  fireEvent.click(section.getByRole('button', { name: 'Скачать отчёт' }));
  await waitFor(() => assert.equal(downloaded, 'mom-performance-14d.json'));
  assert.ok(calls.includes('/api/admin/diagnostics/export?days=14'));
});

test('diagnostics ignores stale period responses and aborts polling on close', async () => {
  let resolveOld, oldSignal, currentSignal;
  mockApi((url, options) => {
    if (url === '/api/admin/diagnostics?days=7') {
      oldSignal = options.signal;
      return new Promise(resolve => { resolveOld = resolve; });
    }
    assert.equal(url, '/api/admin/diagnostics?days=1');
    currentSignal = options.signal;
    return Response.json({ ...summary, days: 1, runs: 22 });
  });
  const view = render(React.createElement(MonitoringPanel));
  await waitFor(() => assert.ok(resolveOld));
  fireEvent.change(view.getByRole('combobox', { name: 'Период отчёта' }), { target: { value: '1' } });
  await view.findByText('22');
  assert.equal(oldSignal.aborted, true);
  await act(async () => resolveOld(Response.json(summary)));
  const section = within(view.getByRole('region', { name: 'Диагностика производительности' }));
  assert.equal(section.queryByText('12'), null);
  view.unmount();
  assert.equal(currentSignal.aborted, true);
});

test('diagnostic storage failure leaves live monitoring available and can be retried', async () => {
  let healthy = false;
  mockApi(() => Response.json(healthy ? summary : {
    ...summary, available: false, runs: 0, stages: {}, recent_runs: [], error: 'storage_unavailable',
  }));
  const view = render(React.createElement(MonitoringPanel));
  await view.findByText(/Диагностика временно недоступна/);
  assert.ok(view.getByRole('heading', { name: 'Ресурсы' }));
  assert.equal(view.getByRole('button', { name: 'Скачать отчёт' }).disabled, true);
  healthy = true;
  fireEvent.click(view.getByRole('button', { name: 'Обновить диагностику' }));
  await view.findByText('12');
  assert.equal(view.queryByText(/Диагностика временно недоступна/), null);
});

test('a late report response cannot trigger a download after the panel closes', async () => {
  let finishExport, exportSignal, clicks = 0;
  mockApi((url, options) => {
    if (url.includes('/export?')) {
      exportSignal = options.signal;
      return new Promise(resolve => { finishExport = resolve; });
    }
    return Response.json(summary);
  });
  URL.createObjectURL = () => { clicks += 100; return 'blob:late'; };
  window.HTMLAnchorElement.prototype.click = () => { clicks += 1; };
  const view = render(React.createElement(MonitoringPanel));
  await view.findByText('12');
  fireEvent.click(view.getByRole('button', { name: 'Скачать отчёт' }));
  await waitFor(() => assert.ok(finishExport));
  view.unmount();
  assert.equal(exportSignal.aborted, true);
  await act(async () => finishExport(Response.json({ schema_version: 1 })));
  assert.equal(clicks, 0);
});

test('diagnostic download handles missing admin session without downloading an error body', async () => {
  const expired = [];
  const unsubscribe = onSessionExpired(scope => expired.push(scope));
  let urls = 0;
  globalThis.fetch = async () => Response.json({ detail: 'Нужен вход' }, { status: 401 });
  URL.createObjectURL = () => { urls += 1; return 'blob:error'; };
  try {
    await assert.rejects(downloadDiagnostics(7, new AbortController().signal), error => error.status === 401);
    assert.deepEqual(expired, ['admin']);
    assert.equal(urls, 0);
  } finally { unsubscribe(); }
});
