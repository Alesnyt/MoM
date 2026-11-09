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
const { cleanup, fireEvent, render, waitFor } = await import('@testing-library/react');
const { AdminSection } = loadSource('admin.tsx');
const originalFetch = globalThis.fetch;
const health = { ffmpeg: true, openai: { configured: true, connected: true, chat_model: 'openai/gpt-5', asr_model: 'local-whisper' } };

afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
});

function renderSettings() {
  return render(React.createElement(AdminSection, {
    auth: { configured: true, authenticated: true },
    health, tab: 'llm', onTab() {}, onAuth() {}, onHealth() {},
  }));
}

test('searches anywhere in model names and keeps selection until a model is chosen', async () => {
  const models = ['openai/gpt-5', 'google/gemini-flash', 'google/gemini-flash-lite', 'anthropic/claude-sonnet'];
  let catalogRequests = 0;
  const saves = [];
  globalThis.fetch = async (url, options) => {
    if (url === '/api/settings/chat-models') {
      catalogRequests++;
      return Response.json({ models });
    }
    assert.equal(url, '/api/settings/models');
    saves.push(JSON.parse(options.body));
    return Response.json(health);
  };
  const view = renderSettings();
  const search = await view.findByRole('searchbox', { name: 'Поиск модели' });
  const select = view.getByRole('combobox', { name: 'Модель чата' });
  const selectableModels = () => Array.from(select.options).filter((item) => !item.disabled).map((item) => item.value);
  assert.deepEqual(selectableModels(), models);

  fireEvent.change(search, { target: { value: '  FLASH  ' } });
  assert.deepEqual(selectableModels(), ['google/gemini-flash', 'google/gemini-flash-lite']);
  assert.equal(select.value, 'openai/gpt-5');
  assert.equal(fireEvent.keyDown(search, { key: 'Enter' }), false);
  assert.deepEqual(saves, []);

  fireEvent.change(select, { target: { value: 'google/gemini-flash-lite' } });
  fireEvent.change(search, { target: { value: 'not-in-catalog' } });
  assert.match(view.getByRole('status').textContent, /Модели не найдены/);
  assert.equal(select.disabled, true);
  assert.equal(select.value, 'google/gemini-flash-lite');
  fireEvent.change(search, { target: { value: '' } });
  assert.deepEqual(selectableModels(), models);
  assert.equal(catalogRequests, 1);
  fireEvent.click(view.getByRole('button', { name: 'Сохранить модель' }));
  await waitFor(() => assert.deepEqual(saves, [{ chat_model: 'google/gemini-flash-lite', asr_model: 'local-whisper' }]));
});

test('keeps manual entry when the provider has no catalog', async () => {
  globalThis.fetch = async () => Response.json({ models: [], manual: true });
  const view = renderSettings();
  await view.findByText(/Каталог моделей недоступен/);
  const field = view.getByRole('textbox', { name: 'Модель чата' });
  assert.equal(field.value, 'openai/gpt-5');
  fireEvent.change(field, { target: { value: 'provider/custom-model' } });
  assert.equal(field.value, 'provider/custom-model');
  assert.equal(view.queryByRole('searchbox'), null);
});
