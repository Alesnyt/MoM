import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
Object.assign(globalThis, { window: dom.window, document: dom.window.document,
  navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement, Node: dom.window.Node });
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

function open() {
  function App() {
    const [meeting, update] = React.useState(initial);
    const [tab, onTab] = React.useState('overview');
    return React.createElement(MeetingPane, { meeting, onUpdated: update, tab, onTab });
  }
  const view = render(React.createElement(App));
  fireEvent.click(view.getByRole('button', { name: 'Редактировать протокол' }));
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
  const view = open();
  assert.equal(view.getByLabelText('Участник 1').value, 'Иван');
  fireEvent.click(view.getByText('Добавить решение'));
  fireEvent.change(view.getByLabelText('Решение 1'), { target: { value: 'Утвердить план' } });
  fireEvent.click(view.getByText('Проверить русский язык'));
  const reason = await view.findByText('Нужен дательный падеж.');
  fireEvent.click(within(reason.parentElement).getByText('Принять'));
  fireEvent.click(view.getByText('Пропустить'));
  assert.equal(view.getByLabelText('Саммари').value, 'Согласно решению. По итогов встречи.');
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
  const view = open();
  fireEvent.change(view.getByLabelText('Саммари'), { target: { value: 'Мой черновик' } });
  fireEvent.click(view.getByText('Проверить русский язык'));
  assert.equal((await view.findByRole('alert')).textContent, 'Модель недоступна');
  fireEvent.click(view.getByText('Сохранить протокол'));
  await waitFor(() => assert.equal(view.getByRole('alert').textContent, 'Протокол уже изменился'));
  assert.equal(view.getByLabelText('Саммари').value, 'Мой черновик');
  window.confirm = () => false;
  fireEvent.click(view.getByText('Отмена'));
  assert.ok(view.getByRole('form'));
  window.confirm = () => true;
  fireEvent.click(view.getByText('Отмена'));
  assert.equal(view.queryByRole('form'), null);
  assert.ok(view.getByText(initial.result.summary));
});

test('archive updates during editing keep the draft paired with the revision it came from', async () => {
  let sent;
  globalThis.fetch = async (_, options) => {
    sent = JSON.parse(options.body);
    return reply({ detail: 'Протокол уже изменился' }, 409);
  };
  const props = { tab: 'mom', onTab() {}, onUpdated() {} };
  const view = render(React.createElement(MeetingPane, { ...props, meeting: initial }));
  fireEvent.click(view.getByText('Редактировать протокол'));
  fireEvent.change(view.getByLabelText('Саммари'), { target: { value: 'Моя версия' } });
  view.rerender(React.createElement(MeetingPane, { ...props, meeting: { ...initial, status: 'queued', result: null } }));
  view.rerender(React.createElement(MeetingPane, { ...props, meeting: { ...initial, result_revision: 'revision2',
    result: { ...initial.result, summary: 'Протокол собран заново' } } }));
  assert.equal(view.getByLabelText('Саммари').value, 'Моя версия');
  fireEvent.click(view.getByText('Сохранить протокол'));
  await view.findByRole('alert');
  assert.equal(sent.revision, 'revision1');
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
