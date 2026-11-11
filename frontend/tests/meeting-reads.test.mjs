import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/'});
Object.assign(globalThis, { window:dom.window, document:dom.window.document,
  navigator:dom.window.navigator, HTMLElement:dom.window.HTMLElement, Node:dom.window.Node });
const React = await import('react');
const {render,fireEvent,waitFor,cleanup,act} = await import('@testing-library/react');
const {default: App} = loadSource('App.tsx');
const {resolveAppearance} = loadSource('appearance.ts');
const originalFetch = globalThis.fetch;
afterEach(() => { cleanup(); globalThis.fetch=originalFetch; });
const response = value => new Response(JSON.stringify(value), {headers:{'Content-Type':'application/json'}});
const row = (id,status='review') => ({id,title:`Встреча ${id}`,filename:'demo.wav',created_at:'2026-11-03T10:00:00Z',
  status,status_message:status==='review'?'Проверьте спикеров':'Старая обработка',duration_seconds:60,language:'ru',
  transcript:null,segments:null,speakers:null,result:null,error:null});
const full = id => ({...row(id),source_type:'audio',transcript:'Готовый текст',speakers:[],segments:[]});
function mockApi(handler) {
  globalThis.fetch = async (url,options={}) => {
    if(url==='/api/health')return response({ok:true,ffmpeg:true,openai:{configured:true,connected:true,message:'OK'},smtp:{configured:false}});
    if(url==='/api/auth/status')return response({configured:true,authenticated:false});
    if(url==='/api/session')return response({authenticated:true,email:'test@example.com',archive_limit:5});
    return handler(url,options);
  };
}

test('appearance switches at local time boundaries and manual selection survives a reload', async () => {
  const at = (hour, minute = 0) => new Date(2026, 10, 11, hour, minute);
  assert.equal(resolveAppearance('auto', at(6, 59)), 'dark');
  assert.equal(resolveAppearance('auto', at(7)), 'light');
  assert.equal(resolveAppearance('auto', at(18, 59)), 'light');
  assert.equal(resolveAppearance('auto', at(19)), 'dark');
  assert.equal(resolveAppearance('light', at(23)), 'light');
  mockApi(() => response([]));
  const view = render(React.createElement(App));
  try {
    fireEvent.change(view.getByRole('combobox', {name: 'Тема'}), {target: {value: 'dark'}});
    assert.equal(document.documentElement.dataset.appearance, 'dark');
    view.unmount();
    const reloaded = render(React.createElement(App));
    assert.equal(reloaded.getByRole('combobox', {name: 'Тема'}).value, 'dark');
    fireEvent.change(reloaded.getByRole('combobox', {name: 'Тема'}), {target: {value: 'light'}});
    assert.equal(document.documentElement.dataset.appearance, 'light');
    await act(async () => {});
  } finally {
    window.localStorage.clear();
  }
});

test('review details load once while another job continues polling', async () => {
  let detailCalls=0,finishDetail,polls=0;
  mockApi((url) => {
    if(url==='/api/meetings'){polls++;return response([row('A'),row('B','transcribing')]);}
    if(url==='/api/meetings/A'){detailCalls++;return new Promise(resolve=>{finishDetail=resolve;});}
    throw new Error(`Unexpected ${url}`);
  });
  const view=render(React.createElement(App));
  await waitFor(()=>assert.equal(detailCalls,1));
  assert.ok(view.getByText('Загружаю транскрипт…'));
  await waitFor(()=>assert.ok(polls>=2),{timeout:4000});
  assert.equal(detailCalls,1);
  await act(async()=>finishDetail(response(full('A'))));
  assert.ok(view.getByText('Готовый текст'));
});

test('recognition opens speaker editing and the skip button queues the protocol', async () => {
  let polls = 0;
  let submitted;
  const finished = {
    ...full('A'),
    speakers: [{ id: 'S1', name: 'Спикер 1' }],
    segments: [{ start: 26, end: 30, speaker: 'S1', text: 'Проверим план запуска.' }],
  };
  mockApi((url, options) => {
    if (url === '/api/meetings') return response([row('A', ++polls === 1 ? 'transcribing' : 'review')]);
    if (url === '/api/meetings/A') return response(finished);
    if (url === '/api/meetings/A/protocol') {
      submitted = JSON.parse(options.body);
      return response({ ...finished, status: 'queued', status_message: 'Протокол поставлен в очередь' });
    }
    throw new Error(`Unexpected ${url}`);
  });
  const view = render(React.createElement(App));
  const transcript = await view.findByRole('heading', { name: 'Транскрипт по спикерам' }, { timeout: 4000 });
  assert.ok(view.getByRole('combobox', { name: 'Имя спикера Спикер 1' }));
  assert.equal(view.queryByRole('button', { name: 'Разметить спикеров' }), null);
  const skip = view.getByRole('button', { name: 'Пропустить и собрать протокол' });
  assert.ok(skip.compareDocumentPosition(transcript) & Node.DOCUMENT_POSITION_FOLLOWING);
  fireEvent.click(skip);
  await waitFor(() => assert.deepEqual(submitted, { recipients: [] }));
  assert.equal(view.queryByRole('region', { name: 'Проверка спикеров' }), null);
  assert.ok(view.getByText('Транскрипт сохранён. Когда освободится слот, начнётся сборка протокола.'));
});

test('late status poll cannot undo a successful restart with the same processing id', async () => {
  let polls=0,finishPoll;
  const active=row('A','transcribing');
  mockApi((url) => {
    if(url==='/api/meetings'){
      if(++polls===1)return response([active]);
      return new Promise(resolve=>{finishPoll=resolve;});
    }
    if(url==='/api/meetings/A/restart')return response({...active,status:'queued',status_message:'Новая обработка в очереди'});
    throw new Error(`Unexpected ${url}`);
  });
  window.confirm=()=>true;
  const view=render(React.createElement(App));
  await waitFor(()=>assert.ok(view.getByRole('button',{name:'Запустить заново'})));
  await waitFor(()=>assert.ok(finishPoll),{timeout:4000});
  fireEvent.click(view.getByRole('button',{name:'Запустить заново'}));
  await waitFor(()=>assert.ok(view.getByText('В очереди',{exact:true})));
  await act(async()=>finishPoll(response([active])));
  assert.ok(view.getByText('В очереди',{exact:true}));
  assert.equal(view.queryByText('Старая обработка',{exact:true}),null);
});

test('switching meetings aborts details and ignores late success and late failure', async () => {
  let finishA,signalA;
  mockApi((url,options) => {
    if(url==='/api/meetings')return response([row('A'),row('B')]);
    if(url==='/api/meetings/A'){
      signalA=options.signal;
      return new Promise(resolve=>{finishA=resolve;});
    }
    if(url==='/api/meetings/B')return response(full('B'));
    throw new Error(`Unexpected ${url}`);
  });
  const view=render(React.createElement(App));
  await waitFor(()=>assert.ok(finishA));
  fireEvent.click(view.getByRole('button',{name:/Встреча B Проверьте спикеров/}));
  await waitFor(()=>assert.ok(signalA.aborted));
  await act(async()=>finishA(new Response(JSON.stringify({detail:'Устаревшая ошибка'}),{status:500})));
  assert.equal(view.queryByText('Устаревшая ошибка'),null);
  assert.ok(view.getByRole('heading',{name:'Встреча B'}));
});

test('failed detail load offers an explicit retry and clears the error after success', async () => {
  let calls=0;
  mockApi(url=>{
    if(url==='/api/meetings')return response([row('A')]);
    if(url==='/api/meetings/A')return ++calls===1
      ? new Response(JSON.stringify({detail:'Временный сбой загрузки'}),{status:503}) : response(full('A'));
    throw new Error(`Unexpected ${url}`);
  });
  const view=render(React.createElement(App));
  await waitFor(()=>assert.ok(view.getByText('Временный сбой загрузки')));
  fireEvent.click(view.getByRole('button',{name:'Повторить загрузку'}));
  await waitFor(()=>assert.ok(view.getByText('Готовый текст')));
  assert.equal(calls,2);
  assert.equal(view.queryByText('Временный сбой загрузки'),null);
});

test('a speaker save in another meeting does not discard transcript details', async () => {
  let saveA, loadB;
  const withSpeaker = (id) => ({ ...full(id), speakers: [{ id: 'S1', name: 'Спикер 1' }],
    segments: [{ start: 0, end: 5, speaker: 'S1', text: `Реплика ${id}` }] });
  mockApi((url, options) => {
    if (url === '/api/meetings') return response([row('A'), row('B')]);
    if (url === '/api/meetings/A') return response(withSpeaker('A'));
    if (url === '/api/meetings/A/speakers/S1' && options.method === 'PATCH') return new Promise(resolve => { saveA = resolve; });
    if (url === '/api/meetings/B') return new Promise(resolve => { loadB = resolve; });
    throw new Error(`Unexpected ${url}`);
  });
  const view = render(React.createElement(App));
  const input = await view.findByRole('combobox', { name: 'Имя спикера Спикер 1' });
  fireEvent.change(input, { target: { value: 'Анна' } });
  fireEvent.submit(input.closest('form'));
  await waitFor(() => assert.ok(saveA));
  fireEvent.click(view.getByRole('button', { name: /Встреча B Проверьте спикеров/ }));
  await waitFor(() => assert.ok(loadB));
  await act(async () => saveA(response({ ...withSpeaker('A'), speakers: [{ id: 'S1', name: 'Анна' }] })));
  await act(async () => loadB(response(withSpeaker('B'))));
  assert.ok(view.getByRole('heading', { name: 'Встреча B' }));
  assert.ok(view.getAllByText('Реплика B').length > 0);
  assert.equal(view.queryByText('Загружаю транскрипт…'), null);
});

test('a decision source opens and focuses the corresponding transcript page', async () => {
  const meeting = { ...full('A'), status: 'done', status_message: 'Готово',
    speakers: [{ id: 'S1', name: 'Анна' }, { id: 'S2', name: 'Борис' }],
    segments: Array.from({ length: 60 }, (_, i) => ({ start: i, end: i + 1, speaker: `S${i % 2 + 1}`, text: `Фраза ${i + 1}` })),
    result: { title: 'Встреча A', summary: '', date_hint: null, participants: [], key_points: [],
      decisions: ['Одобрили пилот'], decision_sources: [{ decision: 'Одобрили пилот', turns: [58] }],
      action_items: [], mom: { agenda: [], next_meeting: null } } };
  mockApi(url => {
    if (url === '/api/meetings') return response([row('A', 'done')]);
    if (url === '/api/meetings/A') return response(meeting);
    throw new Error(`Unexpected ${url}`);
  });
  const view = render(React.createElement(App));
  fireEvent.click(await view.findByRole('button', { name: 'Реплика 58' }));
  assert.ok(view.getByText('Реплики 51–60 из 60'));
  assert.equal(document.activeElement, view.getByText('Фраза 58').closest('article'));
});
