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

test('renaming updates all visible turns and preserves speaker examples', async () => {
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
  assert.ok(view.container.querySelector('.speaker-card').textContent.includes('Реплика #0000'));
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

test('5000 turns use bounded pages; search and reassignment retain original segment indexes', async () => {
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
  fireEvent.change(view.getByRole('searchbox'), {target:{value:'#4500'}});
  assert.equal(view.container.querySelectorAll('.turn').length, 1);
  fireEvent.change(view.getByRole('combobox', {name:'Кто говорит эту реплику'}), {target:{value:'S2'}});
  await waitFor(() => assert.deepEqual(assigned, {speaker_id:'S2', segment_indexes:[4500]}));
  await waitFor(() => assert.equal(view.getByRole('combobox', {name:'Кто говорит эту реплику'}).disabled, false));
  assert.equal(view.getByRole('combobox', {name:'Кто говорит эту реплику'}).value, 'S2');
  fireEvent.change(view.getByRole('searchbox'), {target:{value:'несуществующая фраза'}});
  assert.equal(view.getByRole('status').textContent, 'Реплик не найдено');
  fireEvent.change(view.getByRole('searchbox'), {target:{value:''}});
  assert.equal(view.container.querySelectorAll('.turn').length, 50);
});
