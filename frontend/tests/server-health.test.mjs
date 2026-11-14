import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/'});
Object.assign(globalThis, {window:dom.window, document:dom.window.document,
  navigator:dom.window.navigator, HTMLElement:dom.window.HTMLElement, Node:dom.window.Node});
const React = await import('react');
const {render, fireEvent, waitFor, cleanup, act} = await import('@testing-library/react');
const {default: App} = loadSource('App.tsx');
const {useServerHealth} = loadSource('server-health.ts');
const originalFetch = globalThis.fetch;
const originalTimeout = window.setTimeout;
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; window.setTimeout = originalTimeout; });
const healthy = {ok:true, ffmpeg:true, max_upload_bytes:1024 ** 3,
  openai:{configured:true, connected:true, message:'OK'}, smtp:{configured:false}};
function mockApi(healthRequest) {
  globalThis.fetch = async (url, options={}) => {
    if (url === '/api/health') return healthRequest(options);
    if (url === '/api/auth/status') return Response.json({configured:true, authenticated:false});
    if (url === '/api/session') return Response.json({authenticated:true, email:'test@example.com'});
    if (url === '/api/meetings') return Response.json([]);
    if (url === '/api/calendar/status') return Response.json({enabled:false, connected:false});
    throw new Error(`Unexpected ${url}`);
  };
}
const chooseFile = view => fireEvent.change(view.container.querySelector('input[type=file]'),
  {target:{files:[new window.File(['audio'], 'meeting.wav', {type:'audio/wav'})]}});
const ready = view => waitFor(() => assert.equal(view.getByRole('button', {name:'Обработать'}).disabled, false));


test('initial network failure recovers without losing the selected recording or inventing missing capabilities', async () => {
  let calls = 0;
  mockApi(() => { if (++calls === 1) throw new TypeError('offline'); return Response.json(healthy); });
  const view = render(React.createElement(App));
  const retry = await view.findByRole('button', {name:'Проверить подключение'});
  await view.findByRole('button', {name:'Обработать'});
  assert.equal(view.queryByText(/Не найден ffmpeg|Ключ не принят|ключ ещё не подключён/i), null);
  chooseFile(view);
  fireEvent.change(view.getByRole('textbox', {name:'Название встречи'}), {target:{value:'Мой план'}});
  assert.equal(view.getByRole('button', {name:'Обработать'}).disabled, true);
  fireEvent.click(retry);
  await ready(view);
  assert.equal(calls, 2);
  assert.equal(view.getByRole('textbox', {name:'Название встречи'}).value, 'Мой план');
  assert.ok(view.getByText('meeting.wav'));
  assert.equal(view.queryByRole('button', {name:'Проверить подключение'}), null);
});


test('an unsuccessful health request schedules an automatic retry', async () => {
  let retry, calls = 0;
  window.setTimeout = (callback, delay, ...args) => {
    if (delay === 5000) { retry = callback; return 0; }
    return originalTimeout.call(window, callback, delay, ...args);
  };
  mockApi(() => { if (++calls === 1) throw new Error('network'); return Response.json(healthy); });
  const view = render(React.createElement(App));
  await view.findByRole('button', {name:'Проверить подключение'});
  assert.equal(typeof retry, 'function');
  chooseFile(view);
  await act(async () => retry());
  await ready(view);
  assert.equal(calls, 2);
});


test('a later outage preserves real upload limits and recovers on the browser online event', async () => {
  let offline = false;
  mockApi(() => { if (offline) throw new Error('offline'); return Response.json(healthy); });
  const view = render(React.createElement(App));
  await view.findByRole('button', {name:'Обработать'});
  chooseFile(view);
  await ready(view);
  const limit = view.container.querySelector('.drop-kicker').textContent;
  offline = true;
  fireEvent(window, new window.Event('focus'));
  await view.findByRole('button', {name:'Проверить подключение'});
  assert.equal(view.container.querySelector('.drop-kicker').textContent, limit);
  assert.equal(view.getByRole('button', {name:'Обработать'}).disabled, true);
  assert.equal(view.queryByText('Не найден ffmpeg.'), null);
  offline = false;
  fireEvent(window, new window.Event('online'));
  await ready(view);
});


test('focus and online events do not overlap a pending check; unmount aborts it', async () => {
  let signal, finish, calls = 0;
  mockApi(options => {
    calls++; signal = options.signal;
    return new Promise(resolve => { finish = resolve; });
  });
  const view = render(React.createElement(App));
  await waitFor(() => assert.ok(finish));
  fireEvent(window, new window.Event('online'));
  fireEvent(window, new window.Event('focus'));
  assert.equal(calls, 1);
  view.unmount();
  assert.ok(signal.aborted);
  await act(async () => finish(Response.json(healthy)));
  fireEvent(window, new window.Event('focus'));
  assert.equal(calls, 1);
});


test('a late health response cannot undo newer settings', async () => {
  let finish;
  mockApi(() => new Promise(resolve => { finish = resolve; }));
  function Probe() {
    const {health, setHealth} = useServerHealth();
    return React.createElement('button', {onClick:() => setHealth({...healthy, max_upload_bytes:123})},
      String(health?.max_upload_bytes || 0));
  }
  const view = render(React.createElement(Probe));
  fireEvent.click(view.getByRole('button'));
  assert.equal(view.getByRole('button').textContent, '123');
  await act(async () => finish(Response.json(healthy)));
  assert.equal(view.getByRole('button').textContent, '123');
});


test('a hung health request is aborted and becomes retryable', async () => {
  let expire;
  window.setTimeout = (callback, delay, ...args) => {
    if (delay === 10000) { expire = callback; return 0; }
    return originalTimeout.call(window, callback, delay, ...args);
  };
  mockApi(({signal}) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  }));
  const view = render(React.createElement(App));
  await waitFor(() => assert.ok(expire));
  await act(async () => expire());
  assert.ok(await view.findByRole('button', {name:'Проверить подключение'}));
  assert.equal(view.queryByText('Не найден ffmpeg.'), null);
});
