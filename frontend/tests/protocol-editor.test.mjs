import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
Object.assign(globalThis, { window: dom.window, document: dom.window.document,
  navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement, Node: dom.window.Node });
Object.assign(globalThis, { MutationObserver: dom.window.MutationObserver, getComputedStyle: dom.window.getComputedStyle });
dom.window.Range.prototype.getClientRects = () => [];
dom.window.Range.prototype.getBoundingClientRect = () => new dom.window.DOMRect();
function pasteSummary(input, text) {
  input.focus();
  fireEvent.keyDown(input, {key:'a', code:'KeyA', keyCode:65, ctrlKey:true});
  fireEvent.paste(input, { clipboardData: { files: [], getData: type => type === 'text/plain' ? text : '' } });
}
const React = await import('react');
const { render, fireEvent, waitFor, within, cleanup } = await import('@testing-library/react');
const { MeetingPane } = loadSource('meeting.tsx');
const originalFetch = globalThis.fetch;
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; });
const initial = { id: 'one', title: 'План', created_at: '2026-11-20', status: 'done', result_revision: 'revision1',
  speakers: [{ id: 's1', name: 'Иван', job_title: 'Инженер' }], result: {
    title: 'План', summary: 'Согласно решения. По итогов встречи.', participants: ['Иван'], date_hint: null,
    decisions: [], key_points: [], action_items: [], mom: { agenda: [], next_meeting: null } } };
const reply = (value, status = 200) => new Response(JSON.stringify(value), { status });

async function open() {
  function App() {
    const [meeting, update] = React.useState(initial);
    const [tab, onTab] = React.useState('overview');
    return React.createElement(MeetingPane, { meeting, onUpdated: update, tab, onTab });
  }
  const view = render(React.createElement(App));
  fireEvent.click(view.getByRole('button', { name: 'Редактировать протокол' }));
  await view.findByRole('textbox', {name:'Саммари'});
  return view;
}

test('accept one case correction, skip another, add a decision and save through meeting pane', async () => {
  const requests = [];
  globalThis.fetch = async (path, options) => {
    const payload = JSON.parse(options.body);
    requests.push({ path, payload, method: options.method });
    if (path.endsWith('/proofread')) return reply({ suggestions: [
      { field: 'summary', before: 'Согласно решения', after: 'Согласно решению', reason: 'Нужен дательный падеж.' },
      { field: 'summary', before: 'По итогов', after: 'По итогам', reason: 'Форма множественного числа.' },
    ] });
    return reply({ ...initial, result_revision: 'revision2', result: payload.result });
  };
  const view = await open();
  assert.equal(view.getByLabelText('Участник 1').value, 'Иван');
  fireEvent.click(view.getByText('Добавить решение'));
  fireEvent.change(view.getByLabelText('Решение 1'), { target: { value: 'Утвердить план' } });
  fireEvent.click(view.getByText('Проверить русский язык'));
  const reason = await view.findByText('Нужен дательный падеж.');
  fireEvent.click(within(reason.parentElement).getByText('Принять'));
  fireEvent.click(view.getByText('Пропустить'));
  assert.equal(view.getByLabelText('Саммари').textContent, 'Согласно решению. По итогов встречи.');
  assert.equal(requests.length, 1);
  fireEvent.click(view.getByText('Сохранить протокол'));
  await view.findByText('Правки протокола сохранены.');
  assert.deepEqual(requests.map(r => r.method), ['POST', 'PATCH']);
  assert.equal(requests[1].payload.revision, 'revision1');
  assert.deepEqual(requests[1].payload.result.decisions, ['Утвердить план']);
  assert.ok(view.getByText('Согласно решению. По итогов встречи.'));
});

test('failed proofread or a save conflict retains edits and cancel discards them', async () => {
  globalThis.fetch = async path => path.endsWith('/proofread')
    ? reply({ detail: 'Модель недоступна' }, 502) : reply({ detail: 'Протокол уже изменился' }, 409);
  const view = await open();
  pasteSummary(await view.findByLabelText('Саммари'), 'Мой черновик');
  fireEvent.click(view.getByText('Проверить русский язык'));
  assert.equal((await view.findByRole('alert')).textContent, 'Модель недоступна');
  fireEvent.click(view.getByText('Сохранить протокол'));
  await waitFor(() => assert.equal(view.getByRole('alert').textContent, 'Протокол уже изменился'));
  assert.equal(view.getByLabelText('Саммари').textContent, 'Мой черновик');
  window.confirm = () => false;
  fireEvent.click(view.getByText('Отмена'));
  assert.ok(view.getByRole('form'));
  window.confirm = () => true;
  fireEvent.click(view.getByText('Отмена'));
  assert.equal(view.queryByRole('form'), null);
  assert.ok(view.getByText(initial.result.summary));
});

test('archive updates preserve the draft and require explicit resolution against the new revision', async () => {
  let sent;
  globalThis.fetch = async (_, options) => {
    sent = JSON.parse(options.body);
    return reply({ ...initial, result_revision: 'revision3', result: sent.result });
  };
  const props = { tab: 'mom', onTab() {}, onUpdated() {} };
  const view = render(React.createElement(MeetingPane, { ...props, meeting: initial }));
  fireEvent.click(view.getByText('Редактировать протокол'));
  pasteSummary(await view.findByLabelText('Саммари'), 'Моя версия');
  view.rerender(React.createElement(MeetingPane, { ...props, meeting: { ...initial, status: 'queued', result: null } }));
  view.rerender(React.createElement(MeetingPane, { ...props, meeting: { ...initial, result_revision: 'revision2',
    result: { ...initial.result, summary: 'Протокол собран заново', decisions: ['Новое решение'] } } }));
  assert.equal(view.getByLabelText('Саммари').textContent, 'Моя версия');
  assert.equal(view.getByText('Сохранить протокол').disabled, true);
  assert.ok(view.getByText('Протокол собран заново'));
  fireEvent.click(view.getByText('Перенести мои изменённые разделы в новую версию'));
  assert.equal(view.getByLabelText('Решение 1').value, 'Новое решение');
  fireEvent.click(view.getByText('Сохранить протокол'));
  await view.findByText('Правки протокола сохранены.');
  assert.equal(sent.revision, 'revision2');
  assert.equal(sent.result.summary, 'Моя версия');
});

test('a manual protocol is rebuilt only after confirming the displayed revision', () => {
  const requested = [];
  const meeting = { ...initial, transcript: 'Текст', source_type: 'audio',
    result: { ...initial.result, edited_manually: true } };
  const view = render(React.createElement(MeetingPane, { meeting, tab: 'overview', onRetry: rev => requested.push(rev) }));
  const rebuild = view.getByRole('button', { name: 'Собрать протокол' });
  const original = window.confirm;
  try {
    window.confirm = () => false;
    fireEvent.click(rebuild);
    assert.deepEqual(requested, []);
    window.confirm = () => true;
    fireEvent.click(rebuild);
    assert.deepEqual(requested, ['revision1']);
  } finally {
    window.confirm = original;
  }
});


test('final delivery confirms participants, directory contacts and manual addresses together', async () => {
  if (!globalThis.crypto) globalThis.crypto = (await import('node:crypto')).webcrypto;
  const sent = [];
  globalThis.fetch = async (url, options) => {
    if (url.startsWith('/api/people?')) return reply({people: [
      {email:'ad@example.com', name:'Коллега из региона', source:'ldap'},
    ]});
    assert.equal(url, '/api/meetings/one/protocol/send');
    sent.push(JSON.parse(options.body));
    // A lost acknowledgement must preserve the same confirmation on retry.
    if (sent.length === 1) return reply({detail:'Ответ не получен'}, 503);
    return reply({...initial, mail_pending:true});
  };
  function App() {
    const [meeting, update] = React.useState({...initial,
      speakers:[{id:'s1', name:'Анна', email:'anna@example.com'}]});
    return React.createElement(MeetingPane, {meeting, onUpdated:update, tab:'overview',
      ownerEmail:'owner@example.com', directoryEnabled:true, mailEnabled:true, onTab() {}});
  }
  const view = render(React.createElement(App));
  fireEvent.click(view.getByText('Редактировать протокол'));
  assert.equal(view.queryByRole('region', {name:'Отправка протокола по почте'}), null);
  fireEvent.click(view.getByText('Отмена'));
  const card = within(view.getByRole('region', {name:'Отправка протокола по почте'}));
  assert.match(card.getByRole('list', {name:'Получатели письма'}).textContent, /owner@example.com/);
  assert.equal(card.getByRole('button', {name:'Подтвердить и отправить'}).disabled, false);
  fireEvent.click(card.getByText('Изменить получателей'));
  assert.equal(card.queryByRole('button', {name:'Подтвердить и отправить'}), null);
  fireEvent.click(view.getByRole('button',{name:'Убрать owner@example.com'}));
  fireEvent.click(view.getByRole('button',{name:/Анна · anna@example.com/}));
  const input = view.getByLabelText('Найти в AD или ввести email');
  fireEvent.change(input, {target:{value:'Коллега'}});
  fireEvent.click(await view.findByText('Коллега из региона · ad@example.com'));
  for (const value of ['Extra@example.com', 'extra@example.com']) {
    fireEvent.change(input, {target:{value}});
    fireEvent.click(view.getByRole('button',{name:'Добавить адрес'}));
  }
  fireEvent.click(view.getByText('Применить список'));
  assert.deepEqual(sent, []);
  assert.equal(view.container.querySelectorAll('.delivery-addresses li').length, 3);
  fireEvent.click(view.getByText('Изменить получателей'));
  fireEvent.click(view.getByRole('button',{name:'Убрать ad@example.com'}));
  fireEvent.click(view.getByText('Отмена'));
  assert.equal(view.container.querySelectorAll('.delivery-addresses li').length, 3);
  assert.deepEqual(sent, []);
  fireEvent.click(view.getByText('Подтвердить и отправить'));
  await view.findByText('Ответ не получен');
  fireEvent.click(view.getByText('Подтвердить и отправить'));
  await view.findByText('Письмо ожидает завершения доставки.');
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[0], sent[1]);
  assert.match(sent[0].request_id, /^[a-f0-9]{32}$/);
  assert.equal(sent[0].revision, initial.result_revision);
  assert.deepEqual(sent[0].recipients, ['anna@example.com', 'ad@example.com', 'extra@example.com']);
});

test('an empty recipient list can be edited and cannot be sent', () => {
  globalThis.fetch = () => assert.fail('editing the list must not send a request');
  const view = render(React.createElement(MeetingPane, {meeting:initial, tab:'overview', mailEnabled:true}));
  const button = () => view.getByRole('button', {name:'Подтвердить и отправить'});
  assert.ok(button().disabled);
  fireEvent.click(view.getByText('Добавить получателей'));
  const input = view.getByLabelText('Добавить email получателя');
  fireEvent.change(input, {target:{value:'test@example.com'}});
  assert.ok(view.getByText('Применить список').disabled);
  fireEvent.click(view.getByText('Добавить адрес'));
  fireEvent.click(view.getByText('Применить список'));
  assert.ok(!button().disabled);
  fireEvent.click(view.getByText('Изменить получателей'));
  fireEvent.click(view.getByRole('button',{name:'Убрать test@example.com'}));
  fireEvent.click(view.getByText('Применить список'));
  assert.ok(button().disabled);
});

test('edit the summary before delivery, keep recipients and explicitly confirm the saved revision', async () => {
  if (!globalThis.crypto) globalThis.crypto = (await import('node:crypto')).webcrypto;
  const requests = [];
  const summary = 'Согласовали дату запуска.\n\nОтветственные проверят план.';
  const saved = { ...initial, result_revision: 'revision2', result: { ...initial.result, summary } };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return reply(url.endsWith('/send') ? { ...saved, mail_pending: true } : saved);
  };
  function App() {
    const [meeting, update] = React.useState(initial);
    const [tab, onTab] = React.useState('overview');
    return React.createElement(MeetingPane, { meeting, onUpdated: update, tab, onTab, mailEnabled: true, ownerEmail: 'owner@example.com' });
  }
  const view = render(React.createElement(App));
  const card = () => within(view.getByRole('region', { name: 'Отправка протокола по почте' }));
  fireEvent.click(card().getByText('Изменить получателей'));
  fireEvent.change(view.getByLabelText('Добавить email получателя'), { target: { value: 'extra@example.com' } });
  fireEvent.click(view.getByText('Добавить адрес'));
  fireEvent.click(view.getByText('Применить список'));
  fireEvent.click(card().getByText('Редактировать саммари'));
  const input = await view.findByRole('textbox', { name: 'Саммари' });
  assert.equal(input, document.activeElement);
  assert.equal(input.textContent, initial.result.summary);
  assert.equal(view.queryByText('Подтвердить и отправить'), null);
  pasteSummary(input, summary);
  fireEvent.click(view.getByText('Сохранить саммари'));
  await view.findByText('Саммари сохранено.');
  assert.deepEqual(requests, [{ url: '/api/meetings/one/protocol', body: { revision: 'revision1', summary, summary_format: 'markdown' } }]);
  assert.ok(view.getByText('Ответственные проверят план.'));
  assert.match(card().getByRole('list').textContent, /extra@example.com/);
  fireEvent.click(card().getByText('Подтвердить и отправить'));
  await view.findByText('Письмо ожидает завершения доставки.');
  assert.equal(requests.length, 2);
  assert.equal(requests[1].url, '/api/meetings/one/protocol/send');
  assert.equal(requests[1].body.revision, 'revision2');
  assert.deepEqual(requests[1].body.recipients, ['owner@example.com', 'extra@example.com']);
});

test('summary can be edited and discarded from the overview with mail disabled', async () => {
  globalThis.fetch = () => assert.fail('discarding the draft must not save it');
  const view = render(React.createElement(MeetingPane, { meeting: initial, tab: 'overview', onTab() {}, mailEnabled: false }));
  fireEvent.click(view.getByText('Редактировать саммари'));
  assert.equal(view.queryByLabelText('Название встречи'), null);
  pasteSummary(await view.findByLabelText('Саммари'), 'Черновик');
  const previous = window.confirm;
  try {
    window.confirm = () => true;
    fireEvent.click(view.getByText('Отмена'));
    assert.ok(view.getByText(initial.result.summary));
  } finally { window.confirm = previous; }
});

test('format a long summary with nested lists, undo and restore emphasis, then reopen the saved version', async () => {
  const source = readFileSync(new URL('../../tests/fixtures/summary-pilot.md', import.meta.url), 'utf8');
  let persisted;
  globalThis.fetch = async (_, options) => {
    const { summary, summary_format } = JSON.parse(options.body);
    persisted = { ...initial.result, summary, summary_format };
    return reply({ ...initial, result_revision: 'revision2', result: persisted });
  };
  function App() {
    const [meeting, onUpdated] = React.useState(initial);
    return React.createElement(MeetingPane, {meeting, onUpdated, tab:'overview', onTab() {}});
  }
  const view = render(React.createElement(App));
  const edit = () => fireEvent.click(view.getByText('Редактировать саммари'));
  edit();
  const input = await view.findByRole('textbox', {name:'Саммари'});
  pasteSummary(input, source);
  assert.equal(input.querySelectorAll('h3').length, 5);
  assert.equal(input.querySelectorAll('ol ol, ul ul').length, 2);
  assert.equal(input.querySelectorAll('li').length, 12);
  // Select an entire new paragraph using the same paste and toolbar actions as the browser.
  fireEvent.keyDown(input, {key:'End', keyCode:35, ctrlKey:true});
  fireEvent.keyDown(input, {key:'Enter', keyCode:13});
  fireEvent.click(view.getByRole('button', {name:'Полужирный'}));
  pasteAtCursor(input, 'Контрольная отметка');
  assert.match(input.innerHTML, /<strong>Контрольная отметка<\/strong>/);
  fireEvent.click(view.getByRole('button', {name:'Отменить правку'}));
  assert.doesNotMatch(input.textContent, /Контрольная отметка/);
  fireEvent.click(view.getByRole('button', {name:'Повторить правку'}));
  fireEvent.click(view.getByText('Сохранить саммари'));
  await view.findByText('Саммари сохранено.');
  assert.equal(persisted.summary_format, 'markdown');
  const visible = view.container.querySelector('.summary-content');
  assert.equal(visible.querySelectorAll('li').length, 12);
  assert.match(visible.innerHTML, /<strong>Контрольная отметка<\/strong>/);
  edit();
  assert.equal(view.getByRole('textbox').querySelectorAll('li').length, 12);
});

function pasteAtCursor(input, text) {
  fireEvent.paste(input, { clipboardData: { files: [], getData: type => type === 'text/plain' ? text : '' } });
}
