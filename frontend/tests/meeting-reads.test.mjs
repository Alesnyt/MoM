import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/'});
Object.assign(globalThis, { window:dom.window, document:dom.window.document,
  navigator:dom.window.navigator, HTMLElement:dom.window.HTMLElement, Node:dom.window.Node });
const React = await import('react');
const {render,fireEvent,waitFor,cleanup,act} = await import('@testing-library/react');
const {default: App} = loadSource('App.tsx');
const originalFetch = globalThis.fetch;
afterEach(() => { cleanup(); globalThis.fetch=originalFetch; });
const response = value => new Response(JSON.stringify(value), {headers:{'Content-Type':'application/json'}});
const row = (id,status='review') => ({id,title:`Встреча ${id}`,filename:'demo.wav',created_at:'2026-11-03T10:00:00Z',
  status,status_message:status==='review'?'Проверьте спикеров':'Старая обработка',duration_seconds:60,language:'ru',
  transcript:null,segments:null,speakers:null,result:null,error:null});
const full = id => ({...row(id),source_type:'audio',transcript:'Готовый текст',speakers:[],segments:[]});
function mockApi(handler) {
  globalThis.fetch = async (url,options={}) => {
    if(url==='/api/health')return response({ok:true,ffmpeg:true,openai:{configured:true,connected:true,message:'OK'},smtp:{configured:false}});
    if(url==='/api/auth/status')return response({configured:true,authenticated:false});
    if(url==='/api/session')return response({authenticated:true,email:'test@example.com',archive_limit:5});
    return handler(url,options);
  };
}

test('review details load once while another job continues polling', async () => {
  let detailCalls=0,finishDetail,polls=0;
  mockApi((url) => {
    if(url==='/api/meetings'){polls++;return response([row('A'),row('B','transcribing')]);}
    if(url==='/api/meetings/A'){detailCalls++;return new Promise(resolve=>{finishDetail=resolve;});}
    throw new Error(`Unexpected ${url}`);
  });
  const view=render(React.createElement(App));
  await waitFor(()=>assert.equal(detailCalls,1));
  fireEvent.click(view.getByRole('button',{name:'Разметить спикеров'}));
  assert.ok(view.getByText('Загружаю транскрипт…'));
  await waitFor(()=>assert.ok(polls>=2),{timeout:4000});
  assert.equal(detailCalls,1);
  await act(async()=>finishDetail(response(full('A'))));
  assert.ok(view.getByText('Готовый текст'));
});

test('late status poll cannot undo a successful restart with the same processing id', async () => {
  let polls=0,finishPoll;
  const active=row('A','transcribing');
  mockApi((url) => {
    if(url==='/api/meetings'){
      if(++polls===1)return response([active]);
      return new Promise(resolve=>{finishPoll=resolve;});
    }
    if(url==='/api/meetings/A/restart')return response({...active,status:'queued',status_message:'Новая обработка в очереди'});
    throw new Error(`Unexpected ${url}`);
  });
  window.confirm=()=>true;
  const view=render(React.createElement(App));
  await waitFor(()=>assert.ok(view.getByRole('button',{name:'Запустить заново'})));
  await waitFor(()=>assert.ok(finishPoll),{timeout:4000});
  fireEvent.click(view.getByRole('button',{name:'Запустить заново'}));
  await waitFor(()=>assert.ok(view.getByText('В очереди',{exact:true})));
  await act(async()=>finishPoll(response([active])));
  assert.ok(view.getByText('В очереди',{exact:true}));
  assert.equal(view.queryByText('Старая обработка',{exact:true}),null);
});

test('switching meetings aborts details and ignores late success and late failure', async () => {
  let finishA,signalA;
  mockApi((url,options) => {
    if(url==='/api/meetings')return response([row('A'),row('B')]);
    if(url==='/api/meetings/A'){
      signalA=options.signal;
      return new Promise(resolve=>{finishA=resolve;});
    }
    if(url==='/api/meetings/B')return response(full('B'));
    throw new Error(`Unexpected ${url}`);
  });
  const view=render(React.createElement(App));
  await waitFor(()=>assert.ok(finishA));
  fireEvent.click(view.getByRole('button',{name:/Встреча B Проверьте спикеров/}));
  await waitFor(()=>assert.ok(signalA.aborted));
  await act(async()=>finishA(new Response(JSON.stringify({detail:'Устаревшая ошибка'}),{status:500})));
  assert.equal(view.queryByText('Устаревшая ошибка'),null);
  assert.ok(view.getByRole('heading',{name:'Встреча B'}));
});

test('failed detail load offers an explicit retry and clears the error after success', async () => {
  let calls=0;
  mockApi(url=>{
    if(url==='/api/meetings')return response([row('A')]);
    if(url==='/api/meetings/A')return ++calls===1
      ? new Response(JSON.stringify({detail:'Временный сбой загрузки'}),{status:503}) : response(full('A'));
    throw new Error(`Unexpected ${url}`);
  });
  const view=render(React.createElement(App));
  await waitFor(()=>assert.ok(view.getByText('Временный сбой загрузки')));
  fireEvent.click(view.getByRole('button',{name:'Разметить спикеров'}));
  fireEvent.click(view.getByRole('button',{name:'Повторить загрузку'}));
  await waitFor(()=>assert.ok(view.getByText('Готовый текст')));
  assert.equal(calls,2);
  assert.equal(view.queryByText('Временный сбой загрузки'),null);
});
