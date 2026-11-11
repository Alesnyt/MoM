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
