import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';
const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/'});
Object.assign(globalThis,{window:dom.window,document:dom.window.document,navigator:dom.window.navigator,
  HTMLElement:dom.window.HTMLElement,Node:dom.window.Node});
const React=await import('react');
const {render,fireEvent,waitFor,cleanup,act}=await import('@testing-library/react');
const {CalendarPanel}=loadSource('calendar.tsx');
const {Composer}=loadSource('composer.tsx');
const {ExchangePanel}=loadSource('admin-exchange.tsx');
const {TranscriptView}=loadSource('transcript.tsx');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const event={id:'cal-1',title:'План DWH',start:'2026-11-12T09:00:00Z',end:'2026-11-12T10:00:00Z',location:'Переговорка 3',all_day:false,cancelled:false,private:false};
const detail={...event,agenda:'Обсудить качество данных',invitees:[{name:'Анна Иванова',email:'anna@example.com',kind:'required'}],
  resources:[{name:'Переговорка 3',email:'room@example.com',kind:'resource'}]};
const reply=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
function mockCalendar(extra=()=>{}) {
  globalThis.fetch=async(url,options)=>{
    if(url==='/api/calendar/status') return reply({enabled:true,connected:true,username:'owner@example.com'});
    if(url.startsWith('/api/calendar/events?')) return reply({events:[event],truncated:false});
    if(url==='/api/calendar/events/cal-1') return reply(detail);
    return extra(url,options);
  };
}

test('loads event details only after choosing an event',async()=>{
  let reads=0;const chosen=[];
  globalThis.fetch=async url=>{
    if(url==='/api/calendar/status') return reply({enabled:true,connected:true});
    if(url.includes('/events?')) return reply({events:[event,{...event,id:'cancelled',title:'Отменённая',cancelled:true}],truncated:false});
    reads++;assert.equal(url,'/api/calendar/events/cal-1');return reply(detail);
  };
  const view=render(React.createElement(CalendarPanel,{userEmail:'owner@example.com',onSelect:v=>chosen.push(v)}));
  const button=await view.findByRole('button',{name:/План DWH/});
  assert.equal(reads,0);assert.equal(view.getByRole('button',{name:/Отменённая/}).disabled,true);
  fireEvent.click(button);
  await waitFor(()=>assert.equal(chosen.at(-1)?.agenda,detail.agenda));
  assert.equal(reads,1);assert.equal(chosen.at(-1).resources[0].name,'Переговорка 3');
});

test('selected invitation supplies title and token to transcript upload',async()=>{
  mockCalendar();const uploads=[];
  const view=render(React.createElement(Composer,{busy:false,keyStatus:null,mailReady:false,userEmail:'owner@example.com',onUpload:async(...args)=>uploads.push(args)}));
  fireEvent.click(await view.findByRole('button',{name:/План DWH/}));
  await waitFor(()=>assert.equal(view.getByRole('textbox',{name:'Название встречи'}).value,'План DWH'));
  assert.ok(view.getByText(/Приглашение не подтверждает участие/));
  fireEvent.click(view.getByRole('button',{name:'Готовый транскрипт'}));
  fireEvent.change(view.getByLabelText('Текст транскрипта'),{target:{value:'Спикер 1: Обсудили план.'}});
  fireEvent.click(view.getByRole('button',{name:'Импортировать транскрипт'}));
  await waitFor(()=>assert.deepEqual(uploads,[[null,'План DWH','Спикер 1: Обсудили план.','cal-1']]));
  fireEvent.click(view.getByRole('button',{name:'Убрать привязку'}));
  assert.equal(view.getByRole('textbox',{name:'Название встречи'}).value,'');
});

test('late event response cannot replace a newer selection',async()=>{
  const pending=[];const chosen=[];
  globalThis.fetch=async(url,options)=>{
    if(url==='/api/calendar/status')return reply({enabled:true,connected:true});
    if(url.includes('/events?'))return reply({events:[event,{...event,id:'cal-2',title:'План BI'}],truncated:false});
    return new Promise(resolve=>pending.push({url,signal:options.signal,resolve}));
  };
  const view=render(React.createElement(CalendarPanel,{userEmail:'owner@example.com',onSelect:v=>chosen.push(v)}));
  fireEvent.click(await view.findByRole('button',{name:/План DWH/}));
  fireEvent.click(view.getByRole('button',{name:/План BI/}));
  assert.equal(pending[0].signal.aborted,true);
  await act(async()=>pending[1].resolve(reply({...detail,id:'cal-2',title:'План BI'})));
  await act(async()=>pending[0].resolve(reply(detail)));
  assert.equal(chosen.at(-1).id,'cal-2');
});

test('cancel login disconnects server session and ignores its late success',async()=>{
  let pending,disconnects=0;
  globalThis.fetch=async(url,options)=>{
    if(url==='/api/calendar/status')return reply({enabled:true,connected:false});
    if(url==='/api/calendar/connection'){disconnects++;return reply({connected:false});}
    assert.equal(url,'/api/calendar/connect');
    return new Promise(resolve=>{pending={resolve,signal:options.signal};});
  };
  const view=render(React.createElement(CalendarPanel,{userEmail:'owner@example.com',onSelect(){}}));
  fireEvent.change(await view.findByLabelText('Пароль Exchange'),{target:{value:'local-test-only'}});
  fireEvent.click(view.getByRole('button',{name:'Подключить календарь'}));
  assert.equal(view.getByLabelText('Пароль Exchange').value,'');
  fireEvent.click(await view.findByRole('button',{name:'Отменить'}));
  await waitFor(()=>assert.equal(disconnects,1));
  assert.equal(pending.signal.aborted,true);
  await act(async()=>pending.resolve(reply({enabled:true,connected:true})));
  assert.ok(view.getByRole('button',{name:'Подключить календарь'}));
});

test('expired calendar session offers reconnect and retains the error',async()=>{
  globalThis.fetch=async url=>url==='/api/calendar/status'?reply({enabled:true,connected:true}):reply({detail:'Подключите свой календарь Exchange.'},401);
  const view=render(React.createElement(CalendarPanel,{userEmail:'owner@example.com',onSelect(){}}));
  await view.findByLabelText('Пароль Exchange');
  assert.match(view.getByRole('alert').textContent,/Подключите/);
});

test('admin saves only server settings and normalized EWS address',async()=>{
  const saves=[];
  globalThis.fetch=async(url,options)=>{
    assert.equal(url,'/api/settings/exchange');
    if(options.method==='POST'){saves.push(JSON.parse(options.body));return reply({enabled:true,url:'https://mail.example.com/EWS/Exchange.asmx',auth_mode:'ntlm'});}
    return reply({enabled:false,url:'',auth_mode:'ntlm'});
  };
  const view=render(React.createElement(ExchangePanel));
  fireEvent.click(await view.findByLabelText('Включить календарь Exchange'));
  fireEvent.change(view.getByLabelText('Адрес OWA или EWS'),{target:{value:'https://mail.example.com/owa'}});
  fireEvent.click(view.getByRole('button',{name:'Сохранить Exchange'}));
  await waitFor(()=>assert.equal(saves.length,1));
  assert.deepEqual(saves[0],{enabled:true,url:'https://mail.example.com/owa',auth_mode:'ntlm'});
  await waitFor(()=>assert.equal(view.getByLabelText('Адрес OWA или EWS').value,'https://mail.example.com/EWS/Exchange.asmx'));
});

test('invitees are manual name choices; rooms never become speaker suggestions',async()=>{
  let mutations=0;
  const meeting={id:'m1',title:'План DWH',status:'review',source_type:'audio',transcript:'Спикер 1: Да.',
    speakers:[{id:'S1',name:'Спикер 1'}],segments:[{speaker:'S1',start:0,end:1,text:'Да.'}],calendar_context:{...detail,source:'exchange'}};
  globalThis.fetch=async(url,options)=>{
    mutations++;assert.equal(url,'/api/meetings/m1/speakers/S1');
    assert.equal(JSON.parse(options.body).name,'Анна Иванова');
    return reply({...meeting,speakers:[{id:'S1',name:'Анна Иванова',email:'anna@example.com'}]});
  };
  const view=render(React.createElement(TranscriptView,{meeting,onUpdated(){}}));
  assert.equal(mutations,0);
  fireEvent.focus(view.getByRole('combobox',{name:'Имя спикера Спикер 1'}));
  const option=await view.findByRole('option',{name:/Анна Иванова.*Приглашён/});
  assert.equal(view.queryByRole('option',{name:/Переговорка/}),null);
  fireEvent.click(option);
  await waitFor(()=>assert.equal(mutations,1));
});


test('LDAP calendar connects in background without a second login form',async()=>{
  let polls=0, reads=0;
  globalThis.fetch=async url=>{
    if(url==='/api/calendar/status')return reply(++polls===1
      ? {enabled:true,connected:false,connecting:true,ldap_bound:true,account_email:'owner@example.com'}
      : {enabled:true,connected:true,connecting:false,ldap_bound:true,mailbox:'primary@example.com'});
    assert.ok(url.includes('/events?'));reads++;return reply({events:[event],truncated:false});
  };
  const view=render(React.createElement(CalendarPanel,{userEmail:'owner@example.com',onSelect(){}}));
  await view.findByText(/Подключаю ваш календарь после входа через LDAP/);
  assert.equal(view.queryByLabelText('Пароль Exchange'),null);
  assert.equal(view.queryByLabelText('Доменный логин или email'),null);
  assert.ok(await view.findByRole('button',{name:/План DWH/},{timeout:3000}));
  assert.equal(reads,1);
  assert.ok(view.getByText('Календарь · primary@example.com'));
});

test('LDAP retry cannot select another account and sends only its password',async()=>{
  let body;
  globalThis.fetch=async(url,options)=>{
    if(url==='/api/calendar/status')return reply({enabled:true,connected:false,ldap_bound:true,account_email:'owner@example.com',error:'Exchange недоступен'});
    if(url==='/api/calendar/connect'){body=JSON.parse(options.body);return reply({enabled:true,connected:true,ldap_bound:true});}
    assert.ok(url.includes('/events?'));return reply({events:[],truncated:false});
  };
  const view=render(React.createElement(CalendarPanel,{userEmail:'owner@example.com',onSelect(){}}));
  assert.equal((await view.findByRole('alert')).textContent,'Exchange недоступен');
  assert.equal(view.queryByLabelText('Доменный логин или email'),null);
  fireEvent.change(view.getByLabelText('Пароль Exchange'),{target:{value:'test-secret'}});
  fireEvent.click(view.getByRole('button',{name:'Подключить календарь'}));
  await waitFor(()=>assert.deepEqual(body,{username:'',password:'test-secret'}));
});

test('cancel automatic LDAP connection ignores late status success',async()=>{
  let polls=0,late,disconnects=0;
  globalThis.fetch=async url=>{
    if(url==='/api/calendar/status'){
      if(++polls===1)return reply({enabled:true,connected:false,connecting:true,ldap_bound:true,account_email:'owner@example.com'});
      return new Promise(resolve=>{late=resolve;});
    }
    assert.equal(url,'/api/calendar/connection');disconnects++;return reply({connected:false});
  };
  const view=render(React.createElement(CalendarPanel,{userEmail:'owner@example.com',onSelect(){}}));
  await waitFor(()=>assert.ok(late),{timeout:3000});
  fireEvent.click(view.getByRole('button',{name:'Отменить'}));
  await waitFor(()=>assert.equal(disconnects,1));
  await act(async()=>late(reply({enabled:true,connected:true})));
  assert.ok(view.getByLabelText('Пароль Exchange'));
  assert.equal(view.queryByLabelText('Доменный логин или email'),null);
});
