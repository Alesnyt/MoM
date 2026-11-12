import assert from 'node:assert/strict';
import {afterEach,test} from 'node:test';
import {JSDOM} from 'jsdom';
import {loadSource} from './load-source.mjs';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/admin'});
Object.assign(globalThis,{window:dom.window,document:dom.window.document,navigator:dom.window.navigator,HTMLElement:dom.window.HTMLElement,
  Node:dom.window.Node,FormData:dom.window.FormData});
const React=await import('react');
const {render,fireEvent,waitFor,cleanup}=await import('@testing-library/react');
const {GlossariesPanel}=loadSource('glossaries.tsx');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const catalog={glossaries:[{id:'data',name:'Данные',enabled:true,version:1,term_count:1,mapping:{},sheet:'',filename:'data.csv',updated_at:null}],
  terms:[{id:1,term:'DWH',description:'Хранилище',aliases:['склад'],replacements:[],department:'',glossary_name:'Данные',enabled:true}],
  matched_terms:1,term_count:1,active_term_count:1,jobs:[],revision:1};
const preview={upload_id:'upload',columns:['Название','Смысл','Сокращения'],sample:[['SLA','Уровень обслуживания','эс эл эй']],
  mapping:{term:'Название',description:'Смысл',aliases:'Сокращения'},sheets:[],sheet:'',name:'Бизнес'};

test('mapping preview queues one independent glossary and leaves existing glossary visible',async()=>{
  let queued;
  globalThis.fetch=async(url,options)=>{
    if(String(url).startsWith('/api/admin/glossaries?'))return Response.json(catalog);
    if(url==='/api/admin/glossaries/preview'){assert.equal(options.body.get('glossary_id'),'');return Response.json(preview);}
    assert.equal(url,'/api/admin/glossaries/import');queued=JSON.parse(options.body);return Response.json({status:'queued'},{status:202});
  };
  const view=render(React.createElement(GlossariesPanel));await view.findByText('DWH');
  fireEvent.change(view.getByLabelText('Файл глоссария'),{target:{files:[new window.File(['csv'],'business.csv')]}});
  fireEvent.click(view.getByRole('button',{name:'Проверить файл'}));
  assert.equal((await view.findByLabelText('Термин *')).value,'Название');
  assert.equal(view.getByLabelText('Разрешённые замены в транскрипте').value,'');
  fireEvent.click(view.getByRole('button',{name:'Добавить глоссарий'}));
  await waitFor(()=>assert.equal(queued?.name,'Бизнес'));
  assert.deepEqual(queued.mapping,preview.mapping);
  assert.ok(view.getByText('DWH'));
  assert.ok(await view.findByText(/Импорт поставлен в очередь/));
});

test('update targets only selected glossary and failed toggle preserves checkbox',async()=>{
  globalThis.fetch=async(url,options)=>{
    if(String(url).startsWith('/api/admin/glossaries?'))return Response.json(catalog);
    if(url==='/api/admin/glossaries/preview'){assert.equal(options.body.get('glossary_id'),'data');return Response.json({...preview,name:'Данные'});}
    assert.equal(url,'/api/admin/glossaries/data/enabled');
    return Response.json({detail:'Не удалось сохранить настройку'},{status:503});
  };
  const view=render(React.createElement(GlossariesPanel));await view.findByText('DWH');
  fireEvent.click(view.getByRole('checkbox',{name:'Данные'}));
  await view.findByRole('alert');assert.equal(view.getByRole('checkbox',{name:'Данные'}).checked,true);
  fireEvent.change(view.getByLabelText('Куда загрузить'),{target:{value:'data'}});
  fireEvent.change(view.getByLabelText('Файл глоссария'),{target:{files:[new window.File(['csv'],'data.csv')]}});
  fireEvent.click(view.getByRole('button',{name:'Проверить файл'}));
  assert.ok(await view.findByRole('button',{name:'Обновить этот глоссарий'}));
});

test('background import survives remount and reports failure without hiding previous terms',async()=>{
  let state={...catalog,jobs:[{id:'job',glossary_id:'data',filename:'data.csv',status:'running',received:250,error:null}]};
  globalThis.fetch=async()=>Response.json(state);
  const first=render(React.createElement(GlossariesPanel));await first.findByRole('progressbar');first.unmount();
  const view=render(React.createElement(GlossariesPanel));await view.findByRole('progressbar');
  state={...state,jobs:[{...state.jobs[0],status:'error',error:'Строка 301: термин уже есть в этой области'}]};
  await waitFor(()=>assert.match(view.getByRole('alert').textContent,/Строка 301/),{timeout:3500});
  assert.ok(view.getByText('DWH'));assert.equal(view.queryByRole('progressbar'),null);
});

test('search ignores an obsolete response and shows a visible searching state',async()=>{
  let resolveOld;
  globalThis.fetch=async(url)=>{
    if(String(url).includes('q=old'))return new Promise(resolve=>{resolveOld=resolve;});
    return Response.json(String(url).includes('q=new')?{...catalog,terms:[],matched_terms:0}:catalog);
  };
  const view=render(React.createElement(GlossariesPanel));await view.findByText('DWH');
  const search=view.getByLabelText('Поиск по глоссариям');
  fireEvent.change(search,{target:{value:'old'}});assert.ok(await view.findByText('Поиск…'));
  await waitFor(()=>assert.ok(resolveOld));
  fireEvent.change(search,{target:{value:'new'}});
  await view.findByText(/Совпадений нет/);
  resolveOld(Response.json(catalog));
  await new Promise(resolve=>setTimeout(resolve,50));assert.equal(view.queryByText('DWH'),null);
});
