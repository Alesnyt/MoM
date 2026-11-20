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
