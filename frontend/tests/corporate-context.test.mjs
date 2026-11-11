import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {JSDOM} from 'jsdom';
import {loadSource} from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/admin'});
Object.assign(globalThis, {window:dom.window, document:dom.window.document, navigator:dom.window.navigator,
  HTMLElement:dom.window.HTMLElement, Node:dom.window.Node, FormData:dom.window.FormData});
const React = await import('react');
const {render, fireEvent, waitFor, cleanup} = await import('@testing-library/react');
const {CorporateContextPanel} = loadSource('corporate-context.tsx');
const originalFetch = globalThis.fetch;
afterEach(() => {cleanup(); globalThis.fetch = originalFetch;});
const initial = {enabled:true,sync_ad:true,revision:1,term_count:1,people_count:20,directory_current:true,
  directory_policy:{fields:['job_title','department','surname'],domains:[]},sync_time:'03:00',sync_timezone:'UTC+3',next_sync_at:'2026-11-12T00:00:00Z',
  ldap_configured:true,synced_at:null,imported_at:null,sync_error:null,syncing:false,matched_terms:1,
  terms:[{term:'TrueConf',aliases:['труконф'],description:'Видеоконференции',department:''}]};

test('dictionary import errors remain visible and leave the previous dictionary usable', async () => {
  let imports = 0, reads = 0;
  globalThis.fetch = async (url, options) => {
    if (String(url).startsWith('/api/admin/context?q=')) {reads++; return Response.json(initial);}
    assert.equal(url, '/api/admin/context/import');
    assert.equal(options.body.get('file').name, 'terms.csv');
    imports++;
    return Response.json({detail:'Нужен столбец «Термин»'}, {status:400});
  };
  const view = render(React.createElement(CorporateContextPanel));
  await view.findByText('TrueConf');
  fireEvent.change(view.getByLabelText('Файл словаря'), {target:{files:[new window.File(['bad'], 'terms.csv')]}});
  fireEvent.submit(view.getByRole('button', {name:'Загрузить словарь'}).closest('form'));
  await waitFor(() => assert.equal(imports, 1));
  await waitFor(() => assert.ok(reads > 1));
  assert.equal(view.getByRole('alert').textContent, 'Нужен столбец «Термин»');
  assert.ok(view.getByText('TrueConf'));
});

test('admin can persist a daily sync time without changing it during other settings updates', async () => {
  let current = {...initial}, writes = [];
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/context/settings') {
      const body = JSON.parse(options.body); writes.push(body); current = {...current,...body};
    }
    return Response.json(current);
  };
  const view = render(React.createElement(CorporateContextPanel));
  const input = await view.findByLabelText('Время запуска');
  assert.equal(input.value, '03:00');
  assert.ok(view.getByText(/московскому времени \(UTC\+3\)/));
  assert.match(view.getByText(/Ближайший запуск/).textContent, /03:00/);
  fireEvent.change(input, {target:{value:'23:15'}});
  fireEvent.submit(view.getByRole('button', {name:'Сохранить время'}).closest('form'));
  await waitFor(() => assert.equal(current.sync_time, '23:15'));
  const toggle = view.getByRole('checkbox', {name:/Обновлять AD автоматически/});
  await waitFor(() => assert.equal(toggle.disabled, false));
  fireEvent.click(toggle);
  await waitFor(() => assert.equal(current.sync_ad, false));
  assert.equal(current.sync_time, '23:15');
  assert.equal(writes[1].sync_time, undefined);
  assert.ok(await view.findByText(/Автоматическая синхронизация выключена/));
});

test('admin can disable context and trigger directory synchronization', async () => {
  let current = {...initial}, synced = false;
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/context/settings') current = {...current, ...JSON.parse(options.body)};
    else if (url === '/api/admin/context/sync') {synced = true; current = {...current,people_count:25};}
    else assert.ok(String(url).startsWith('/api/admin/context?q='));
    return Response.json(current);
  };
  const view = render(React.createElement(CorporateContextPanel));
  const enabled = await view.findByRole('checkbox', {name:/Использовать корпоративный/});
  fireEvent.click(enabled);
  await waitFor(() => assert.equal(current.enabled, false));
  await waitFor(() => assert.equal(view.getByRole('button', {name:'Синхронизировать AD'}).disabled, false));
  fireEvent.click(view.getByRole('button', {name:'Синхронизировать AD'}));
  await waitFor(() => assert.ok(synced));
  assert.ok(await view.findByText(/Сотрудников в контексте — 25/));
});

test('directory field and domain choices survive refreshes and are applied before a sync', async () => {
  let current = {...initial}, saved, syncs = 0;
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/context/directory') {
      saved = JSON.parse(options.body);
      current = {...current, directory_policy:saved, directory_current:false,people_count:0};
    } else if (url === '/api/admin/context/sync') {
      syncs++; assert.deepEqual(saved.fields, ['department','surname']);
      current = {...current,directory_current:true,people_count:12};
    }
    return Response.json(current);
  };
  const view = render(React.createElement(CorporateContextPanel));
  const jobTitle = await view.findByRole('checkbox', {name:'Должность (title)'});
  assert.equal(jobTitle.checked, true);
  fireEvent.click(jobTitle);
  fireEvent.change(view.getByLabelText('Разрешённые почтовые домены'), {target:{value:'example.com; other.ru\ncompany.ru'}});
  fireEvent.submit(view.getByRole('button', {name:'Сохранить поля и домены'}).closest('form'));
  await waitFor(() => assert.deepEqual(saved.domains, ['example.com','other.ru','company.ru']));
  assert.ok(await view.findByText(/Справочник нужно обновить/));
  await waitFor(() => assert.equal(view.getByRole('button', {name:'Синхронизировать AD'}).disabled, false));
  fireEvent.click(view.getByRole('button', {name:'Синхронизировать AD'}));
  await waitFor(() => assert.equal(syncs, 1));
  await view.findByText(/Сотрудников в контексте — 12/);
  assert.equal(jobTitle.checked, false);
});
