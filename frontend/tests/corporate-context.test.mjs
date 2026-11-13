import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {JSDOM} from 'jsdom';
import {loadSource} from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/admin'});
Object.assign(globalThis, {window:dom.window, document:dom.window.document, navigator:dom.window.navigator,
  HTMLElement:dom.window.HTMLElement, Node:dom.window.Node, FormData:dom.window.FormData});
const React = await import('react');
const {render, fireEvent, waitFor, cleanup, act} = await import('@testing-library/react');
const {CorporateContextPanel} = loadSource('corporate-context.tsx');
const {DirectoryCachePanel} = loadSource('directory-cache.tsx');
const originalFetch = globalThis.fetch;
afterEach(() => {cleanup(); globalThis.fetch = originalFetch;});
const initial = {enabled:true,use_ad:true,sync_ad:true,revision:1,term_count:1,people_count:20,directory_current:true,
  search_mode:'live',search_cache_ready:false,
  directory_policy:{fields:['job_title','department','surname'],domains:[]},sync_time:'03:00',sync_timezone:'UTC+3',next_sync_at:'2026-11-12T00:00:00Z',
  ldap_configured:true,synced_at:null,imported_at:null,sync_error:null,syncing:false,matched_terms:1,
  terms:[{term:'TrueConf',aliases:['труконф'],description:'Видеоконференции',department:''}]};

const discovery = {job:{enabled:true,run_time:'03:30',max_batches:100,next_run_at:'2026-11-13T00:30:00Z',status:'idle',batches:0,found:0,pending_meetings:0},counts:{},proposals:[],total:0};
const vocabulary = {glossaries:[], jobs:[],term_count:1,active_term_count:1,matched_terms:1,revision:1,
  terms:initial.terms.map(e=>({...e,id:1,glossary_name:'Корпоративный',enabled:true,replacements:[]}))};

test('dictionary preview errors remain visible and leave previous terms usable', async () => {
  let previews=0;
  globalThis.fetch=async (url,options)=> {
    if(String(url).startsWith('/api/admin/term-discovery?'))return Response.json(discovery);
    if(String(url).startsWith('/api/admin/context?q='))return Response.json(initial);
    if(String(url).startsWith('/api/admin/glossaries?'))return Response.json(vocabulary);
    assert.equal(url,'/api/admin/glossaries/preview');
    assert.equal(options.body.get('file').name,'terms.csv');previews++;
    return Response.json({detail:'Не удалось прочитать CSV'},{status:400});
  };
  const view=render(React.createElement(CorporateContextPanel));
  await view.findByText('TrueConf');
  fireEvent.change(view.getByLabelText('Файл глоссария'),{target:{files:[new window.File(['bad'],'terms.csv')]}});
  fireEvent.click(view.getByRole('button',{name:'Проверить файл'}));
  await waitFor(()=>assert.equal(previews,1));
  assert.equal((await view.findByRole('alert')).textContent,'Не удалось прочитать CSV');
  assert.ok(view.getByText('TrueConf'));
});

test('admin can persist a daily sync time without changing it during other settings updates', async () => {
  let current = {...initial}, writes = [];
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/directory/settings') {
      const body = JSON.parse(options.body); writes.push(body); current = {...current,...body};
    }
    return Response.json(current);
  };
  const view = render(React.createElement(DirectoryCachePanel));
  const input = await view.findByLabelText('Время запуска');
  assert.equal(input.value, '03:00');
  assert.ok(view.getByText(/московскому времени \(UTC\+3\)/));
  assert.match(view.getByText(/Ближайший запуск/).textContent, /03:00/);
  await act(async () => {}); // Finish loading the saved schedule before editing it.
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

test('context controls consumption only; toggling AD does not change cache or search settings', async () => {
  let current = {...initial}, writes = [];
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/context/settings') {
      const body = JSON.parse(options.body); writes.push(body); current = {...current, ...body};
    } else if(String(url).startsWith('/api/admin/term-discovery?')) return Response.json(discovery);
    else if(String(url).startsWith('/api/admin/glossaries?')) return Response.json(vocabulary);
    else assert.ok(String(url).startsWith('/api/admin/context?q='));
    return Response.json(current);
  };
  const view = render(React.createElement(CorporateContextPanel));
  fireEvent.click(await view.findByRole('checkbox', {name:/Использовать сотрудников AD/}));
  await waitFor(() => assert.equal(current.use_ad, false));
  assert.deepEqual(writes, [{enabled:true,use_ad:false}]);
  assert.equal(current.sync_ad, true);
  assert.equal(view.queryByRole('button', {name:'Синхронизировать AD'}), null);
  assert.equal(view.queryByRole('radio', {name:'Локальный кэш AD'}), null);
  assert.equal(view.queryByLabelText('Время запуска'), null);
  assert.ok(await view.findByText('TrueConf'));
});

test('directory field and domain choices survive refreshes and are applied before a sync', async () => {
  let current = {...initial}, saved, syncs = 0;
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/directory/policy') {
      saved = JSON.parse(options.body);
      current = {...current, directory_policy:saved, directory_current:false,people_count:0};
    } else if (url === '/api/admin/directory/sync') {
      syncs++; assert.deepEqual(saved.fields, ['department','surname']);
      current = {...current,directory_current:true,people_count:12};
    }
    return Response.json(current);
  };
  const view = render(React.createElement(DirectoryCachePanel));
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
  await view.findByText(/Сотрудников в кэше — 12/);
  assert.equal(jobTitle.checked, false);
});

test('live is default; successful sync unlocks cache but only an explicit choice changes the mode', async () => {
  let current = {...initial}, writes = [];
  globalThis.fetch = async (url, options) => {
    if (url === '/api/admin/directory/sync') current = {...current,search_cache_ready:true,synced_at:'2026-11-11T15:00:00Z'};
    if (url === '/api/admin/directory/search') {
      const {mode} = JSON.parse(options.body); writes.push(mode); current = {...current,search_mode:mode};
    }
    return Response.json(current);
  };
  const view = render(React.createElement(DirectoryCachePanel));
  const live = await view.findByRole('radio', {name:'Живой AD (по умолчанию)'});
  const cache = view.getByRole('radio', {name:'Локальный кэш AD'});
  assert.equal(live.checked,true); assert.equal(cache.disabled,true);
  fireEvent.click(view.getByRole('button',{name:'Синхронизировать AD'}));
  await waitFor(() => assert.equal(cache.disabled,false));
  assert.equal(live.checked,true); assert.deepEqual(writes,[]);
  fireEvent.click(cache);
  await waitFor(() => assert.equal(cache.checked,true));
  await waitFor(() => assert.equal(live.disabled,false));
  fireEvent.click(live);
  await waitFor(() => assert.equal(live.checked,true));
  assert.deepEqual(writes,['cache','live']);
});

test('failed mode change keeps the previous selection and shows the error', async () => {
  globalThis.fetch = async (url) => url === '/api/admin/directory/search'
    ? Response.json({detail:'Сначала успешно синхронизируйте AD с текущими настройками'},{status:400})
    : Response.json({...initial,search_cache_ready:true});
  const view = render(React.createElement(DirectoryCachePanel));
  fireEvent.click(await view.findByRole('radio',{name:'Локальный кэш AD'}));
  assert.match((await view.findByRole('alert')).textContent,/Сначала успешно синхронизируйте AD/);
  assert.equal(view.getByRole('radio',{name:'Живой AD (по умолчанию)'}).checked,true);
});

test('directory progress survives remount, updates in background and keeps failed counts visible', async () => {
  let current = {...initial, syncing:true, sync_progress:{status:'running',phase:'reading',received:500,pages:1,
    scopes_done:0,scopes_total:2,current_base:'OU=Staff,DC=example,DC=com'}};
  globalThis.fetch = async url => {
    assert.equal(url, '/api/admin/directory/cache');
    return Response.json(current);
  };
  const first = render(React.createElement(DirectoryCachePanel));
  await first.findByText(/Получено сотрудников: 500/);
  assert.equal(first.getByRole('progressbar').getAttribute('value'), null);
  assert.equal(first.getByRole('button', {name:'Синхронизация выполняется…'}).disabled, true);
  first.unmount();
  const view = render(React.createElement(DirectoryCachePanel));
  await view.findByText(/Получено сотрудников: 500/);
  current = {...current,syncing:false,sync_error:'AD не завершил выгрузку: timeLimitExceeded (код 3)',
    sync_progress:{...current.sync_progress,status:'error',received:1000,pages:2}};
  await waitFor(() => assert.match(view.getByRole('alert').textContent,/timeLimitExceeded/), {timeout:3500});
  assert.ok(view.getByText(/Получено сотрудников: 1000/));
  assert.equal(view.queryByRole('progressbar'), null);
  assert.equal(view.getByRole('button', {name:'Синхронизировать AD'}).disabled, false);
});
