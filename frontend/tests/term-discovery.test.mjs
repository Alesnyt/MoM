import assert from 'node:assert/strict';
import {afterEach,test} from 'node:test';
import {JSDOM} from 'jsdom';
import {loadSource} from './load-source.mjs';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/admin'});
Object.assign(globalThis,{window:dom.window,document:dom.window.document,navigator:dom.window.navigator,HTMLElement:dom.window.HTMLElement,Node:dom.window.Node});
const React=await import('react');
const {render,fireEvent,waitFor,cleanup}=await import('@testing-library/react');
const {TermDiscoveryPanel}=loadSource('term-discovery.tsx');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const variant={id:'v1',revision:1,variant:'файн би',canonical:'',description:'',department:'',replace_text:0,state:'pending',meetings:3,
  examples:[{id:1,title:'Отчётность',excerpt:'Открой файн би',start:5,audio:true}]};
const catalog={job:{enabled:true,run_time:'03:30',max_batches:100,next_run_at:'2026-11-13T00:30:00Z',status:'idle',batches:0,found:0,pending_meetings:2},
  counts:{pending:2},proposals:[{key:'fnb',meetings:3,variants:[variant,{...variant,id:'v2',variant:'Fine B',examples:[]}]}],total:1};

test('requires selected variants and explicit spelling; correction is opt-in',async()=>{
  let saved;
  globalThis.fetch=async(url,opts)=>{
    if(String(url).startsWith('/api/admin/term-discovery?'))return Response.json(catalog);
    if(String(url).startsWith('/api/admin/glossaries?'))return Response.json({terms:[]});
    saved=JSON.parse(opts.body);return Response.json({ok:true});
  };
  const view=render(React.createElement(TermDiscoveryPanel));
  const approve=await view.findByRole('button',{name:'Подтвердить выбранные'});assert.equal(approve.disabled,true);
  assert.equal(view.getByRole('button',{name:'На проверке. Вариантов — 2'}).textContent,'На проверке · 2');
  assert.ok(view.getByText(/Варианты со сходным звучанием/));
  assert.equal(view.queryByRole('button',{name:/Обычное слово/}),null);
  assert.equal(view.getByLabelText('Правильное написание').value,'');
  fireEvent.click(view.getByRole('checkbox',{name:/^файн би/}));
  assert.equal(approve.disabled,true);
  fireEvent.change(view.getByLabelText('Правильное написание'),{target:{value:'FineBI'}});
  fireEvent.click(approve);await waitFor(()=>assert.ok(saved));
  assert.deepEqual(saved.items,[{id:'v1',revision:1}]);assert.equal(saved.replace_text,false);
  assert.equal(saved.canonical,'FineBI');
});

test('a word-form card hides and restores the full family with its original evidence', async () => {
  const family = {key:'ru-project', form_label:'проект', meetings:2, variants:[
    {...variant, id:'first', variant:'проекта', examples:[
      {id:11, title:'Обсуждение сроков', excerpt:'Сроки проекта изменились.', start:65, audio:true}]},
    {...variant, id:'second', variant:'проектами', examples:[]},
  ]};
  const actions = [];
  let hidden = false;
  globalThis.fetch = async (url, options) => {
    const request = new URL(String(url), 'http://localhost');
    if (request.pathname.endsWith('/review')) {
      const action = JSON.parse(options.body);
      actions.push(action);
      hidden = action.action === 'hide_forms';
      return Response.json({ok:true});
    }
    const show = request.searchParams.get('state') === (hidden ? 'ordinary' : 'pending');
    return Response.json({...catalog, counts:{pending:hidden ? 0 : 2, ordinary:hidden ? 2 : 0},
      group_counts:{pending:hidden ? 0 : 1, ordinary:hidden ? 1 : 0},
      total:show ? 1 : 0, proposals:show ? [family] : []});
  };
  const view = render(React.createElement(TermDiscoveryPanel));
  await view.findByRole('heading', {name:'проект'});
  assert.equal(view.getByRole('button',{name:'На проверке. Карточек — 1'}).textContent,'На проверке · 1');
  assert.ok(view.getByText(/Словоформы из реплик/));
  assert.equal(view.getAllByRole('checkbox', {name:/^проект/}).length, 2);
  fireEvent.click(view.getByRole('button', {name:/Обычное слово/}));
  await view.findByText('В этом разделе предложений пока нет.');
  fireEvent.click(view.getByRole('button', {name:'Скрытые слова. Карточек — 1'}));
  const restore = await view.findByRole('button', {name:'Вернуть слово и его формы'});
  assert.equal(view.queryByLabelText('Правильное написание'), null);
  assert.ok(view.getByText('Сроки проекта изменились.'));
  assert.ok(view.getByText('Обсуждение сроков · 1:05'));
  fireEvent.click(restore);
  await view.findByText('В этом разделе предложений пока нет.');
  fireEvent.click(view.getByRole('button', {name:/На проверке/}));
  await view.findByRole('heading', {name:'проект'});
  assert.deepEqual(actions, [
    {action:'hide_forms', items:[{id:'first', revision:1}]},
    {action:'restore_forms', items:[{id:'first', revision:1}]},
  ]);
});

test('failed approval retains selection and draft, retry can enable correction and scope',async()=>{
  let requests=0,saved;
  globalThis.fetch=async(url,opts)=>{
    if(String(url).includes('term-discovery?'))return Response.json(catalog);
    if(String(url).includes('/glossaries?'))return Response.json({terms:[]});
    saved=JSON.parse(opts.body);requests++;
    return requests===1?Response.json({detail:'Предложение уже изменилось'},{status:409}):Response.json({ok:true});
  };
  const view=render(React.createElement(TermDiscoveryPanel));await view.findByLabelText('Правильное написание');
  fireEvent.click(view.getByRole('checkbox',{name:/^файн би/}));
  fireEvent.change(view.getByLabelText('Правильное написание'),{target:{value:'FineBI'}});
  fireEvent.change(view.getByLabelText('Подразделение из AD (необязательно)'),{target:{value:'Данные'}});
  fireEvent.click(view.getByRole('checkbox',{name:/Автоматически заменять/}));
  fireEvent.click(view.getByRole('button',{name:'Подтвердить выбранные'}));await view.findByRole('alert');
  assert.equal(view.getByLabelText('Правильное написание').value,'FineBI');
  fireEvent.click(view.getByRole('button',{name:'Подтвердить выбранные'}));await waitFor(()=>assert.equal(requests,2));
  assert.equal(saved.replace_text,true);assert.equal(saved.department,'Данные');
});

test('schedule persists chosen Moscow time and shows paused progress after remount',async()=>{
  let saved;let data={...catalog,job:{...catalog.job,status:'paused',batches:7,found:2}};
  globalThis.fetch=async(url,opts)=>{
    if(String(url).includes('term-discovery?'))return Response.json(data);
    saved=JSON.parse(opts.body);return Response.json({ok:true});
  };
  let view=render(React.createElement(TermDiscoveryPanel));await view.findByText('Ожидаю завершения обработки встреч');
  fireEvent.change(view.getByLabelText('Время анализа (Москва, UTC+3)'),{target:{value:'22:10'}});
  fireEvent.click(view.getByRole('button',{name:'Сохранить расписание анализа'}));await waitFor(()=>assert.equal(saved.run_time,'22:10'));
  assert.equal(saved.max_batches,100);
  view.unmount();data={...data,job:{...data.job,run_time:'22:10'}};view=render(React.createElement(TermDiscoveryPanel));
  assert.equal((await view.findByLabelText('Время анализа (Москва, UTC+3)')).value,'22:10');
  assert.match(view.getByText(/Порций: 7/).textContent,/Новых вариантов: 2/);
});

test('approved rule can be revoked and audio loads only on explicit playback',async()=>{
  let saved;
  globalThis.fetch=async(url,opts)=>{
    if(String(url).includes('state=approved'))return Response.json({...catalog,proposals:[{key:'fnb',meetings:3,variants:[{...variant,canonical:'FineBI',state:'approved'}]}]});
    if(String(url).includes('term-discovery?'))return Response.json(catalog);
    if(String(url).includes('/glossaries?'))return Response.json({terms:[]});
    saved=JSON.parse(opts.body);return Response.json({ok:true});
  };
  const view=render(React.createElement(TermDiscoveryPanel));await view.findByText('Открой файн би');
  assert.equal(view.container.querySelector('audio'),null);
  fireEvent.click(view.getByRole('button',{name:'Прослушать реплику'}));
  assert.match(view.container.querySelector('audio').src,/evidence\/1\/audio$/);
  fireEvent.click(view.getByRole('button',{name:/Подтверждённые/}));await view.findByRole('button',{name:'Отозвать и пересмотреть'});
  fireEvent.click(view.getByRole('checkbox',{name:/^файн би/}));
  fireEvent.click(view.getByRole('button',{name:'Отозвать и пересмотреть'}));await waitFor(()=>assert.equal(saved.action,'reopen'));
});
