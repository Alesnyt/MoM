import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { JSDOM } from 'jsdom';
import { loadSource } from './load-source.mjs';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url:'http://localhost/'});
Object.assign(globalThis, {window:dom.window, document:dom.window.document,
  navigator:dom.window.navigator, HTMLElement:dom.window.HTMLElement, Node:dom.window.Node});
Object.assign(globalThis, { MutationObserver: dom.window.MutationObserver, getComputedStyle: dom.window.getComputedStyle });
dom.window.Range.prototype.getClientRects = () => [];
dom.window.Range.prototype.getBoundingClientRect = () => new dom.window.DOMRect();
const React = await import('react');
const {render, fireEvent, waitFor, cleanup, act} = await import('@testing-library/react');
const {default: App} = loadSource('App.tsx');
const {MeetingPane} = loadSource('meeting.tsx');
const {apiRequest, ApiError, onSessionExpired, sessionChanged} = loadSource('http.ts');
const originalFetch = globalThis.fetch, originalTimeout = window.setTimeout;
afterEach(() => {
  cleanup(); globalThis.fetch = originalFetch; window.setTimeout = originalTimeout;
  window.history.replaceState({}, '', '/');
});
const healthy = {ok:true, ffmpeg:true, max_upload_bytes:1024 ** 3,
  openai:{configured:true, connected:true, message:'OK'}, smtp:{configured:false}};
const session = {authenticated:true, id:'user-A', email:'test@example.com', archive_limit:5};
const meeting = {id:'A', title:'Сохранённая встреча', filename:'meeting.wav', created_at:'2026-11-14T12:00:00Z',
  status:'done', status_message:'Готово', source_type:'audio', segments:[], transcript:'Текст',
  result:{title:'План',summary:'Саммари',date_hint:null,participants:[],key_points:[],decisions:[],action_items:[],mom:{agenda:[],next_meeting:null}}};
const failure = (status, detail) => Response.json({detail}, {status});
function mockApi(handler, admin = false) {
  globalThis.fetch = async (url, options={}) => {
    if (url === '/api/health') return Response.json(healthy);
    if (url === '/api/auth/status') return Response.json({configured:true,authenticated:admin});
    if (url === '/api/session') return Response.json(session);
    if (url === '/api/calendar/status') return Response.json({enabled:false, connected:false});
    return handler(url, options);
  };
}

test('initial archive failure has an explicit retry and successful retry clears the error', async () => {
  let calls = 0;
  mockApi(url => {
    assert.equal(url, '/api/meetings');
    return ++calls === 1 ? failure(503, 'Архив временно недоступен') : Response.json([meeting]);
  });
  const view = render(React.createElement(App));
  fireEvent.click(await view.findByRole('button', {name:'Повторить загрузку архива'}));
  assert.ok(await view.findByRole('button', {name:/Сохранённая встреча/}));
  assert.equal(calls, 2);
  assert.equal(view.queryByRole('button', {name:'Повторить загрузку архива'}), null);
});

test('archive retries automatically after initial failure even with no known processing meetings', async () => {
  let retry, calls = 0;
  window.setTimeout = (callback, delay, ...args) => {
    if (delay === 5000) { retry = callback; return 0; }
    return originalTimeout.call(window, callback, delay, ...args);
  };
  mockApi(url => {
    assert.equal(url, '/api/meetings');
    return ++calls === 1 ? failure(503, 'Недоступен') : Response.json([meeting]);
  });
  const view = render(React.createElement(App));
  await view.findByRole('button', {name:'Повторить загрузку архива'});
  await act(async () => retry());
  assert.ok(await view.findByRole('button', {name:/Сохранённая встреча/}));
  assert.equal(calls, 2);
});

test('focus and online refresh the archive without overlapping a pending request', async () => {
  let calls = 0, finish;
  mockApi(url => {
    assert.equal(url, '/api/meetings');
    if (++calls === 1) return failure(503, 'Недоступен');
    return new Promise(resolve => { finish = resolve; });
  });
  const view = render(React.createElement(App));
  await view.findByRole('button', {name:'Повторить загрузку архива'});
  fireEvent(window, new window.Event('focus'));
  await waitFor(() => assert.ok(finish));
  fireEvent(window, new window.Event('online'));
  assert.equal(calls, 2);
  await act(async () => finish(Response.json([meeting])));
  assert.ok(await view.findByRole('button', {name:/Сохранённая встреча/}));
});

async function expireDraft(view) {
  fireEvent.click(await view.findByRole('button', {name:'Готовый транскрипт'}));
  fireEvent.change(view.getByRole('textbox', {name:'Текст транскрипта'}), {target:{value:'Анна: План на пятницу.'}});
  fireEvent.change(view.getByRole('textbox', {name:'Название встречи'}), {target:{value:'Мой черновик'}});
  fireEvent.click(view.getByRole('button', {name:'Импортировать транскрипт'}));
  await view.findByRole('heading', {name:'Вход'});
  assert.ok(view.getByText(/Сессия истекла/));
  assert.equal(view.queryByRole('button', {name:'Выйти из профиля'}), null);
}
async function signIn(view, email) {
  fireEvent.change(view.getByPlaceholderText('email@company.com'), {target:{value:email}});
  fireEvent.change(view.getByPlaceholderText('Пароль'), {target:{value:'password'}});
  fireEvent.click(view.getByRole('button', {name:'Войти'}));
  await view.findByRole('button', {name:'Выйти из профиля'});
}

test('expired upload session asks for login and preserves the same user text draft', async () => {
  mockApi(url => {
    if (url === '/api/meetings') return Response.json([meeting]);
    if (url === '/api/meetings/transcript') return failure(401, 'Войдите в профиль');
    if (url === '/api/session/login') return Response.json(session);
    throw new Error(`Unexpected ${url}`);
  });
  const view = render(React.createElement(App));
  await expireDraft(view);
  assert.equal(view.queryByRole('button', {name:/Сохранённая встреча/}), null);
  await signIn(view, session.email);
  assert.equal(view.getByRole('textbox', {name:'Текст транскрипта'}).value, 'Анна: План на пятницу.');
  assert.equal(view.getByRole('textbox', {name:'Название встречи'}).value, 'Мой черновик');
  assert.equal(view.queryByText(/Сессия истекла/), null);
});

for (const sameUser of [true, false]) {
  test('protocol draft survives expiration only for the original account: ' + sameUser, async () => {
    let nextUser = session;
    let saved;
    let expired = false;
    mockApi((url, options) => {
      if (url === '/api/meetings') return Response.json(nextUser.id === session.id ? [{...meeting, result_revision:'r1'}] : []);
      if (url === '/api/session/login') {
        if (!sameUser) nextUser = {...session, id:'user-B'};
        return Response.json(nextUser);
      }
      if (url === '/api/meetings/A/protocol') {
        if (!expired) { expired = true; return failure(401, 'Войдите в профиль'); }
        saved = JSON.parse(options.body);
        return Response.json({...meeting, result_revision:'r2', result:{...meeting.result, summary:saved.summary}});
      }
      throw new Error('Unexpected ' + url);
    });
    const view = render(React.createElement(App));
    fireEvent.click(await view.findByRole('button', {name:/Сохранённая встреча/}));
    fireEvent.click(view.getByRole('button', {name:'Редактировать саммари'}));
    const input = await view.findByRole('textbox', {name:'Саммари'});
    input.focus();
    fireEvent.keyDown(input, {key:'a', code:'KeyA', keyCode:65, ctrlKey:true});
    fireEvent.paste(input, {clipboardData:{files:[],getData:type => type === 'text/plain' ? '**Отредактировано**' : ''}});
    fireEvent.click(view.getByRole('button', {name:'Сохранить саммари'}));
    await view.findByRole('heading', {name:'Вход'});
    await signIn(view, session.email);
    if (sameUser) {
      const restored = await view.findByRole('textbox', {name:'Саммари'});
      assert.equal(restored.querySelector('strong').textContent, 'Отредактировано');
      fireEvent.click(view.getByRole('button', {name:'Сохранить саммари'}));
      await view.findByText('Саммари сохранено.');
      assert.equal(saved.revision, 'r1');
      assert.equal(saved.summary_format, 'markdown');
    } else {
      assert.equal(view.queryByRole('form', {name:'Редактор саммари'}), null);
      assert.equal(view.queryByText('Отредактировано'), null);
    }
  });
}

test('archive focus fetches changed meeting details without downloading unchanged transcripts', async () => {
  let version = 1, details = 0;
  mockApi(url => {
    if (url === '/api/meetings') return Response.json([{ ...meeting, segments:null, transcript:null, result:null, body_version:version }]);
    if (url === '/api/meetings/A') {
      details++;
      return Response.json({ ...meeting, body_version:version, result_revision:'r' + version,
        result:{ ...meeting.result, summary:'Редакция ' + version } });
    }
    throw new Error('Unexpected ' + url);
  });
  const view = render(React.createElement(App));
  fireEvent.click(await view.findByRole('button', {name:/Сохранённая встреча/}));
  await view.findByText('Редакция 1');
  version++;
  fireEvent(window, new window.Event('focus'));
  await view.findByText('Редакция 2');
  await act(async () => fireEvent(window, new window.Event('focus')));
  assert.equal(details, 2);
});

test('logging in as another user clears the previous user draft and archive', async () => {
  let different = false;
  mockApi(url => {
    if (url === '/api/meetings') return Response.json(different ? [] : [meeting]);
    if (url === '/api/meetings/transcript') return failure(401, 'Войдите в профиль');
    if (url === '/api/session/login') { different = true; return Response.json({...session,id:'user-B',email:'other@example.com'}); }
    throw new Error(`Unexpected ${url}`);
  });
  const view = render(React.createElement(App));
  await expireDraft(view);
  await signIn(view, 'other@example.com');
  assert.equal(view.getByRole('textbox', {name:'Название встречи'}).value, '');
  fireEvent.click(view.getByRole('button', {name:'Готовый транскрипт'}));
  assert.equal(view.getByRole('textbox', {name:'Текст транскрипта'}).value, '');
  assert.equal(view.queryByRole('button', {name:/Сохранённая встреча/}), null);
});

test('a recreated account with the same email does not inherit the deleted account draft or archive', async () => {
  let recreated = false;
  mockApi(url => {
    if (url === '/api/meetings') return Response.json(recreated ? [] : [meeting]);
    if (url === '/api/meetings/transcript') return failure(401, 'Войдите в профиль');
    if (url === '/api/session/login') { recreated = true; return Response.json({...session,id:'replacement-user'}); }
    throw new Error(`Unexpected ${url}`);
  });
  const view = render(React.createElement(App));
  await expireDraft(view);
  await signIn(view, session.email);
  assert.equal(view.getByRole('textbox', {name:'Название встречи'}).value, '');
  fireEvent.click(view.getByRole('button', {name:'Готовый транскрипт'}));
  assert.equal(view.getByRole('textbox', {name:'Текст транскрипта'}).value, '');
  assert.equal(view.queryByRole('button', {name:/Сохранённая встреча/}), null);
});

for (const oldOutcome of ['success', 'failure']) {
  test(`late upload ${oldOutcome} after account switch cannot expose old data, erase a draft, or finish a newer upload`, async () => {
    let finishOld, finishNew, uploadCount = 0;
    const other = {...session,id:'user-B',email:'other@example.com'};
    mockApi(url => {
      if (url === '/api/meetings') return Response.json([]);
      if (url === '/api/meetings/transcript') return new Promise(resolve => {
        if (++uploadCount === 1) finishOld = resolve; else finishNew = resolve;
      });
      if (url === '/api/session/logout') return Response.json({authenticated:false});
      if (url === '/api/session/login') return Response.json(other);
      throw new Error(`Unexpected ${url}`);
    });
    const view = render(React.createElement(App));
    fireEvent.click(await view.findByRole('button', {name:'Готовый транскрипт'}));
    fireEvent.change(view.getByRole('textbox', {name:'Текст транскрипта'}), {target:{value:'Секретный текст A'}});
    fireEvent.click(view.getByRole('button', {name:'Импортировать транскрипт'}));
    await waitFor(() => assert.ok(finishOld));
    fireEvent.click(view.getByRole('button', {name:'Выйти из профиля'}));
    await view.findByRole('heading', {name:'Вход'});
    await signIn(view, other.email);
    fireEvent.click(view.getByRole('button', {name:'Готовый транскрипт'}));
    fireEvent.change(view.getByRole('textbox', {name:'Текст транскрипта'}), {target:{value:'Черновик B'}});
    fireEvent.change(view.getByRole('textbox', {name:'Название встречи'}), {target:{value:'Встреча B'}});
    assert.equal(view.getByRole('button', {name:'Импортировать транскрипт'}).disabled, false);
    fireEvent.click(view.getByRole('button', {name:'Импортировать транскрипт'}));
    await waitFor(() => assert.ok(finishNew));
    await act(async () => finishOld(oldOutcome === 'success'
      ? Response.json(meeting) : failure(503, 'Устаревшая ошибка A')));
    assert.equal(view.getByRole('textbox', {name:'Текст транскрипта'}).value, 'Черновик B');
    assert.equal(view.getByRole('textbox', {name:'Название встречи'}).value, 'Встреча B');
    assert.equal(view.getByRole('button', {name:'Загружаю…'}).disabled, true);
    assert.equal(view.queryByRole('button', {name:/Сохранённая встреча/}), null);
    assert.equal(view.queryByText('Устаревшая ошибка A'), null);
    await act(async () => finishNew(Response.json({...meeting,id:'B',title:'Встреча B'})));
    assert.ok(await view.findByRole('heading', {name:'Встреча B'}));
    assert.ok(view.getByText(other.email));
  });
}

test('a late initial session response cannot replace a newer login', async () => {
  const other = {...session,id:'user-B',email:'other@example.com'};
  let finishSession;
  mockApi(url => {
    if (url === '/api/meetings') return Response.json([]);
    if (url === '/api/session/login') return Response.json(other);
    throw new Error(`Unexpected ${url}`);
  });
  const delegate = globalThis.fetch;
  globalThis.fetch = (url, options) => url === '/api/session'
    ? new Promise(resolve => { finishSession = resolve; }) : delegate(url, options);
  const view = render(React.createElement(App));
  await waitFor(() => assert.ok(finishSession));
  await signIn(view, other.email);
  fireEvent.change(view.getByRole('textbox', {name:'Название встречи'}), {target:{value:'Черновик B'}});
  await act(async () => finishSession(Response.json(session)));
  assert.ok(view.getByText(other.email));
  assert.equal(view.getByRole('textbox', {name:'Название встречи'}).value, 'Черновик B');
  assert.equal(view.queryByText(session.email), null);
});

test('a late post-upload session refresh cannot sign the previous user back in after logout', async () => {
  let sessionCalls = 0, finishSession;
  mockApi(url => {
    if (url === '/api/meetings') return Response.json([]);
    if (url === '/api/meetings/transcript') return Response.json(meeting);
    if (url === '/api/session/logout') return Response.json({authenticated:false});
    throw new Error(`Unexpected ${url}`);
  });
  const delegate = globalThis.fetch;
  globalThis.fetch = (url, options) => url === '/api/session' && ++sessionCalls > 1
    ? new Promise(resolve => { finishSession = resolve; }) : delegate(url, options);
  const view = render(React.createElement(App));
  fireEvent.click(await view.findByRole('button', {name:'Готовый транскрипт'}));
  fireEvent.change(view.getByRole('textbox', {name:'Текст транскрипта'}), {target:{value:'Текст A'}});
  fireEvent.click(view.getByRole('button', {name:'Импортировать транскрипт'}));
  await waitFor(() => assert.ok(finishSession));
  fireEvent.click(view.getByRole('button', {name:'Выйти из профиля'}));
  await view.findByRole('heading', {name:'Вход'});
  await act(async () => finishSession(Response.json(session)));
  assert.ok(view.getByRole('heading', {name:'Вход'}));
  assert.equal(view.queryByRole('button', {name:'Выйти из профиля'}), null);
  assert.equal(view.queryByRole('button', {name:/Сохранённая встреча/}), null);
});

test('late protocol update from an old pane cannot overwrite refreshed data after re-login', async () => {
  let finishProtocol, relogged = false;
  const review = {...meeting,status:'review',result:null,speakers:[]};
  mockApi(url => {
    if (url === '/api/meetings') return Response.json([relogged ? meeting : review]);
    if (url === '/api/meetings/A/protocol') return new Promise(resolve => { finishProtocol = resolve; });
    if (url === '/api/session/logout') return Response.json({authenticated:false});
    if (url === '/api/session/login') { relogged = true; return Response.json(session); }
    throw new Error(`Unexpected ${url}`);
  });
  const view = render(React.createElement(App));
  fireEvent.click(await view.findByRole('button', {name:/Сохранённая встреча/}));
  fireEvent.click(await view.findByRole('button', {name:'Пропустить и собрать протокол'}));
  await waitFor(() => assert.ok(finishProtocol));
  fireEvent.click(view.getByRole('button', {name:'Выйти из профиля'}));
  await view.findByRole('heading', {name:'Вход'});
  await signIn(view, session.email);
  fireEvent.click(await view.findByRole('button', {name:/Сохранённая встреча/}));
  assert.ok(await view.findByRole('heading', {name:'Саммари'}));
  await act(async () => finishProtocol(Response.json({...review,status:'queued'})));
  assert.ok(view.getByRole('heading', {name:'Саммари'}));
  assert.equal(view.queryByText('В очереди', {exact:true}), null);
});

test('an admin 401 requests admin login without ending the independent user session', async () => {
  window.history.replaceState({}, '', '/admin');
  mockApi(url => {
    if (url === '/api/meetings') return Response.json([meeting]);
    if (url === '/api/settings') return Response.json(healthy);
    if (url === '/api/admin/monitoring') return failure(401, 'Нужна авторизация администратора');
    throw new Error(`Unexpected ${url}`);
  }, true);
  const view = render(React.createElement(App));
  await view.findByText('Сессия администратора истекла. Войдите снова.');
  assert.ok(view.getByRole('button', {name:'Войти'}));
  assert.ok(view.getByRole('button', {name:'Выйти из профиля'}));
  assert.ok(view.getByRole('button', {name:/Сохранённая встреча/}));
});

test('temporary admin settings failure offers retry instead of logging the administrator out', async () => {
  window.history.replaceState({}, '', '/admin');
  let calls = 0;
  mockApi(url => {
    if (url === '/api/meetings') return Response.json([]);
    if (url === '/api/settings') return ++calls === 1 ? failure(503, 'Настройки недоступны') : Response.json(healthy);
    if (url === '/api/admin/monitoring') return failure(503, 'Мониторинг недоступен');
    throw new Error(`Unexpected ${url}`);
  }, true);
  const view = render(React.createElement(App));
  fireEvent.click(await view.findByRole('button', {name:'Повторить загрузку настроек'}));
  await waitFor(() => assert.equal(view.queryByRole('button', {name:'Повторить загрузку настроек'}), null));
  assert.ok(view.getByRole('button', {name:'Выйти', exact:true}));
  assert.equal(view.queryByRole('button', {name:'Войти'}), null);
  assert.equal(calls, 2);
});

test('HTTP errors retain status, login rejection does not expire a session, and stale 401 cannot expire a new login', async () => {
  const expired = [];
  const stop = onSessionExpired(scope => expired.push(scope));
  try {
    globalThis.fetch = async () => failure(401, 'Неверный пароль');
    await assert.rejects(apiRequest('/api/session/login'), err => err instanceof ApiError && err.status === 401);
    assert.deepEqual(expired, []);
    let finish;
    globalThis.fetch = () => new Promise(resolve => { finish = resolve; });
    const old = apiRequest('/api/meetings');
    sessionChanged('user');
    finish(failure(401, 'Старая сессия'));
    await assert.rejects(old, ApiError);
    assert.deepEqual(expired, []);
  } finally { stop(); }
});

test('Exchange 401 prompts calendar reconnect only while the MoM session is still valid', async () => {
  const expired = [];
  const stop = onSessionExpired(scope => expired.push(scope));
  let authenticated = true;
  globalThis.fetch = async url => url === '/api/session'
    ? Response.json({authenticated}) : failure(401, 'Подключите календарь');
  try {
    await assert.rejects(apiRequest('/api/calendar/events'), err => err instanceof ApiError && !err.session);
    assert.deepEqual(expired, []);
    authenticated = false;
    await assert.rejects(apiRequest('/api/calendar/events'), err => err.session === 'user');
    assert.deepEqual(expired, ['user']);
  } finally { stop(); }
});

test('copy protocol reports an API failure without an unhandled rejection or clipboard warning', async () => {
  globalThis.fetch = async () => failure(503, 'Протокол временно недоступен');
  const view = render(React.createElement(MeetingPane, {meeting, tab:'overview', mailEnabled:false,
    onTab:()=>{}, onRetry:()=>{}, onStop:()=>{}, onRestart:()=>{}, onDelete:()=>{}, onUpdated:()=>{}}));
  fireEvent.click(view.getByRole('button', {name:'Копировать', exact:true}));
  assert.ok(await view.findByText('Протокол временно недоступен'));
  assert.equal(view.queryByText(/Разрешите сайту доступ/), null);
});
