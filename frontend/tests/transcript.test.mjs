import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
Object.assign(globalThis, { window: dom.window, document: dom.window.document,
  navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement, Node: dom.window.Node });
const React = await import('react');
const { render, fireEvent, waitFor, cleanup, act } = await import('@testing-library/react');
const { TranscriptView } = loadSource('transcript.tsx');
const originalFetch = globalThis.fetch;
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; });

function meeting(count = 3) {
  return { id: 'review', title: 'Встреча', status: 'review', source_type: 'audio',
    transcript: 'Полный текст', speakers: [{ id: 'S1', name: 'Спикер 1' }, { id: 'S2', name: 'Спикер 2' }],
    segments: Array.from({length: count}, (_, i) => ({ start: i * 2, end: i * 2 + 1.8,
      speaker: `S${i % 2 + 1}`, text: `Реплика #${String(i).padStart(4, '0')}` })) };
}
function mount(initial) {
  function Harness() {
    const [value, setValue] = React.useState(initial);
    return React.createElement(TranscriptView, { meeting: value, onUpdated: setValue });
  }
  return render(React.createElement(Harness));
}
const response = (value, status = 200) => new Response(JSON.stringify(value), { status,
  headers: { 'Content-Type': 'application/json' } });

test('loading, empty, plain-text and untimed transcripts stay distinct', () => {
  const value = meeting();
  const view = render(React.createElement(TranscriptView, {
    meeting: {...value, segments: null, transcript: null}, onUpdated() {},
  }));
  assert.equal(view.getByRole('status').textContent, 'Загружаю транскрипт…');
  view.rerender(React.createElement(TranscriptView, {
    meeting: {...value, segments: [], transcript: ''}, onUpdated() {},
  }));
  assert.ok(view.getByText('Транскрипт пуст'));
  view.rerender(React.createElement(TranscriptView, {
    meeting: {...value, segments: [], transcript: 'Исходная расшифровка'}, onUpdated() {},
  }));
  assert.ok(view.getByText('Исходная расшифровка'));
  view.rerender(React.createElement(TranscriptView, {
    meeting: {...value, segments: value.segments.map(s => ({...s, start:null, end:null}))}, onUpdated() {},
  }));
  assert.equal(view.container.querySelectorAll('time').length, 0);
  assert.equal(view.container.querySelectorAll('.turn').length, 3);
});

test('renaming updates visible turns and keeps the assignment panel free of transcript text', async () => {
  const value = meeting();
  let finish, calls = 0;
  globalThis.fetch = async (url, init) => {
    if (String(url).startsWith('/api/people')) return response({people: []});
    calls++;
    assert.match(String(url), /\/speakers\/S1$/);
    assert.equal(JSON.parse(init.body).name, 'Анна Иванова');
    return new Promise(resolve => { finish = resolve; });
  };
  const view = mount(value);
  const input = view.getByRole('combobox', { name: 'Имя спикера Спикер 1' });
  fireEvent.change(input, {target: {value: 'Анна Иванова'}});
  fireEvent.submit(input.closest('form'));
  await waitFor(() => assert.ok(finish));
  assert.equal(input.disabled, true);
  fireEvent.submit(input.closest('form'));
  assert.equal(calls, 1);
  await act(async () => finish(response({...value, speakers: [
    {id:'S1', name:'Анна Иванова'}, value.speakers[1],
  ]})));
  assert.deepEqual(Array.from(view.container.querySelectorAll('.speaker-name'), e => e.textContent),
    ['Анна Иванова', 'Спикер 2', 'Анна Иванова']);
  const assignment = view.getByRole('region', {name:'Назначение спикеров'});
  assert.doesNotMatch(assignment.textContent, /Реплика #/);
});

test('failed save keeps the draft and allows a successful retry', async () => {
  const value = meeting();
  let calls = 0;
  globalThis.fetch = async url => {
    if (String(url).startsWith('/api/people')) return response({people: []});
    return ++calls === 1 ? response({detail:'Не удалось сохранить имя'}, 500) :
      response({...value, speakers:[{id:'S1', name:'Анна'}, value.speakers[1]]});
  };
  const view = mount(value);
  const input = view.getByRole('combobox', {name:'Имя спикера Спикер 1'});
  fireEvent.change(input, {target:{value:'Анна'}});
  fireEvent.submit(input.closest('form'));
  await waitFor(() => assert.equal(view.getByRole('alert').textContent, 'Не удалось сохранить имя'));
  assert.equal(input.value, 'Анна');
  assert.equal(input.disabled, false);
  fireEvent.submit(input.closest('form'));
  await waitFor(() => assert.ok(view.getByRole('combobox', {name:'Имя спикера Анна'})));
  assert.equal(calls, 2);
  assert.equal(view.queryByRole('alert'), null);
});

test('speaker selection filters all pages and composes with search and reassignment', async () => {
  const value = meeting(5000);
  let assigned;
  globalThis.fetch = async (_url, init) => {
    assigned = JSON.parse(init.body);
    const segments = value.segments.map((s, i) => assigned.segment_indexes.includes(i)
      ? {...s, speaker:assigned.speaker_id} : s);
    return response({...value, segments});
  };
  const view = mount(value);
  assert.equal(view.container.querySelectorAll('.turn').length, 50);
  assert.equal(view.container.querySelectorAll('.turn option').length, 150);
  fireEvent.click(view.getByRole('button', {name:'Следующие реплики'}));
  assert.match(view.getByRole('status').textContent, /51–100 из 5000/);
  assert.match(view.container.querySelector('.turn > p').textContent, /#0050/);
  const firstSpeaker = view.getByRole('button', {name:'Показать реплики: Спикер 1'});
  fireEvent.click(firstSpeaker);
  assert.equal(firstSpeaker.getAttribute('aria-pressed'), 'true');
  assert.match(view.getByRole('status').textContent, /1–50 из 2500/);
  assert.match(view.container.querySelector('.turn > p').textContent, /#0000/);
  assert.ok(Array.from(view.container.querySelectorAll('.turn .speaker-name'), e => e.textContent)
    .every(name => name === 'Спикер 1'));
  fireEvent.change(view.getByRole('searchbox'), {target:{value:'#4500'}});
  assert.equal(view.container.querySelectorAll('.turn').length, 1);
  fireEvent.change(view.getByRole('combobox', {name:'Кто говорит эту реплику'}), {target:{value:'S2'}});
  await waitFor(() => assert.deepEqual(assigned, {speaker_id:'S2', segment_indexes:[4500]}));
  await waitFor(() => assert.equal(view.getByRole('status').textContent, 'Реплик не найдено'));
  fireEvent.click(view.getByRole('button', {name:'Все спикеры'}));
  assert.equal(firstSpeaker.getAttribute('aria-pressed'), 'false');
  assert.equal(view.getByRole('searchbox').value, '#4500');
  await waitFor(() => assert.equal(view.getByRole('combobox', {name:'Кто говорит эту реплику'}).disabled, false));
  assert.equal(view.container.querySelector('.turn .speaker-name').textContent, 'Спикер 2');
  assert.equal(view.getByRole('combobox', {name:'Кто говорит эту реплику'}).value, '');
  fireEvent.change(view.getByRole('searchbox'), {target:{value:'несуществующая фраза'}});
  assert.equal(view.getByRole('status').textContent, 'Реплик не найдено');
  fireEvent.change(view.getByRole('searchbox'), {target:{value:''}});
  assert.equal(view.container.querySelectorAll('.turn').length, 50);
});

test('AD search is explained before typing, stays visible while waiting, and labels directory results', async () => {
  const value = meeting();
  let finish;
  globalThis.fetch = async (url) => {
    assert.match(String(url), /^\/api\/people\?q=/);
    return new Promise(resolve => { finish = resolve; });
  };
  const view = render(React.createElement(TranscriptView, {meeting:value, onUpdated() {}, directoryEnabled:true}));
  assert.equal(view.getAllByText('Найти сотрудника в AD').length, 2);
  assert.match(view.getByRole('region', {name:'Назначение спикеров'}).textContent, /Показать реплики.*транскрипт отфильтруется/);
  const input = view.getByRole('combobox', {name:'Найти сотрудника в AD для Спикер 1'});
  fireEvent.focus(input);
  fireEvent.change(input, {target:{value:'Иван'}});
  assert.ok(view.getByText('Ищем сотрудника в справочнике AD…'));
  assert.equal(input.getAttribute('aria-busy'), 'true');
  await waitFor(() => assert.ok(finish));
  await act(async () => finish(response({people:[{name:'Иван Петров',email:'ivan@example.com',source:'ldap',job_title:'Архитектор'}],ldap:true})));
  assert.equal(view.queryByText('Ищем сотрудника в справочнике AD…'), null);
  assert.equal(input.getAttribute('aria-busy'), 'false');
  const option = view.getByRole('option', {name:/Иван Петров/});
  assert.match(option.textContent, /Иван ПетровADАрхитектор/);
  fireEvent.click(view.getByRole('button', {name:'Показать реплики: Спикер 2'}));
  assert.ok(view.getByText('✓ Реплики показаны · Сбросить'));
  assert.match(view.container.querySelector('.transcript-filter').textContent, /Показаны только реплики: Спикер 2/);
});

test('a failed AD lookup is distinct from no matches and retains saved people', async () => {
  globalThis.fetch = async () => response({ldap:true,error:'Поиск в AD временно недоступен.',
    people:[{name:'saved@example.com',email:'saved@example.com',source:'mom'}]});
  const view = render(React.createElement(TranscriptView, {meeting:meeting(),onUpdated() {},directoryEnabled:true}));
  fireEvent.change(view.getByRole('combobox', {name:'Найти сотрудника в AD для Спикер 1'}), {target:{value:'saved'}});
  assert.ok(await view.findByText('Поиск в AD временно недоступен.'));
  assert.ok(view.getByRole('option', {name:/saved@example.com/}));
  assert.equal(view.queryByText(/В справочнике AD совпадений не найдено/), null);
});

test('a superseded AD request cannot erase newer matches or leave the spinner running', async () => {
  const requests = [];
  globalThis.fetch = (url, options) => new Promise((resolve, reject) => {requests.push({url,options,resolve,reject});});
  const view = render(React.createElement(TranscriptView, {meeting:meeting(),onUpdated() {},directoryEnabled:true}));
  const input = view.getByRole('combobox', {name:'Найти сотрудника в AD для Спикер 1'});
  fireEvent.change(input, {target:{value:'Ан'}});
  await waitFor(() => assert.equal(requests.length, 1));
  fireEvent.change(input, {target:{value:'Иван'}});
  await waitFor(() => assert.equal(requests.length, 2));
  assert.equal(requests[0].options.signal.aborted, true);
  await act(async () => requests[1].resolve(response({ldap:true,people:[{name:'Иван Петров',email:'ivan@example.com',source:'ldap'}]})));
  await act(async () => requests[0].reject(new Error('Old failure')));
  assert.ok(view.getByRole('option', {name:/Иван Петров/}));
  assert.equal(view.queryByText('Ищем сотрудника в справочнике AD…'), null);
  assert.equal(view.queryByText(/Поиск недоступен/), null);
});
