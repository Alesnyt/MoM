import { useEffect, useRef, useState } from "react";
import { calendarEvent, calendarEvents, calendarStatus, connectCalendar, disconnectCalendar } from "./calendar-api";
import type { CalendarContext, CalendarEvent, CalendarStatus } from "./calendar-types";

const dateValue = (date: Date) => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
export function calendarTime(event: Pick<CalendarEvent, "start" | "end" | "all_day">): string {
  const start = new Date(event.start), end = new Date(event.end);
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) return "Время не указано";
  const day = start.toLocaleDateString("ru-RU", { weekday:"short", day:"numeric", month:"short" });
  return event.all_day ? `${day} · весь день` : `${day} · ${start.toLocaleTimeString("ru-RU", { hour:"2-digit", minute:"2-digit" })}–${end.toLocaleTimeString("ru-RU", { hour:"2-digit", minute:"2-digit" })}`;
}

export function CalendarContextCard({ event, onClear }: { event: CalendarEvent | CalendarContext; onClear?: () => void }) {
  return <section className="calendar-context" aria-label="Контекст из приглашения">
    <div className="calendar-context-heading"><span className="eyebrow">Из вашего календаря</span>
      {onClear && <button className="ghost" type="button" onClick={onClear}>Убрать привязку</button>}</div>
    <strong>{event.title}</strong><p>{calendarTime(event)}</p>
    {event.location && <p>Место: {event.location}</p>}
    {!!event.resources?.length && <p>Переговорки и ресурсы: {event.resources.map(p => p.name || p.email).join(", ")}</p>}
    <details><summary>Повестка и приглашённые · {event.invitees?.length ?? 0}</summary>
      {event.organizer && <p>Организатор: {event.organizer.name || event.organizer.email}</p>}
      <p className="progress-hint">Приглашение не подтверждает участие. Спикеров назначаем после распознавания.</p>
      {!!event.invitees?.length && <ul>{event.invitees.map((person,index) => <li key={`${person.email}-${index}`}>
        {person.name || person.email}{person.kind === "optional" ? " · необязательный" : ""}{person.response === "Decline" ? " · отклонил приглашение" : ""}
      </li>)}</ul>}
      <h3>План встречи</h3><p className="calendar-agenda">{event.agenda || "В приглашении нет повестки."}</p>
    </details>
  </section>;
}

export function CalendarPanel({ onSelect, selectedId, userEmail, disabled, onOpenSettings, onPending }: {
  onSelect: (event: CalendarEvent | null) => void; selectedId?: string; userEmail: string;
  disabled?: boolean; onOpenSettings?: () => void; onPending?: (pending: boolean) => void;
}) {
  const [status, setStatus] = useState<CalendarStatus | null>(null);
  const [username, setUsername] = useState(userEmail);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [date, setDate] = useState(() => dateValue(new Date()));
  const [days, setDays] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const loginRequest = useRef<AbortController | null>(null);
  const detailRequest = useRef<AbortController | null>(null);
  const select = useRef(onSelect); select.current = onSelect;

  useEffect(() => {
    const controller = new AbortController();
    calendarStatus(controller.signal).then(setStatus).catch((err: Error) => { if (!controller.signal.aborted) setError(err.message); });
    return () => { controller.abort(); loginRequest.current?.abort(); detailRequest.current?.abort(); };
  }, []);

  useEffect(() => {
    if (!status?.connecting) return;
    const controller = new AbortController();
    let timer: number;
    const poll = () => {
      calendarStatus(controller.signal).then(next => {
        if (controller.signal.aborted) return;
        setStatus(next);
        if (next.connecting) timer = window.setTimeout(poll, 1000);
      }).catch((err: Error) => {
        if (controller.signal.aborted) return;
        setError(err.message);
        setStatus(old => old ? {...old, connecting:false} : old);
      });
    };
    timer = window.setTimeout(poll, 1000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [status?.connecting]);

  useEffect(() => {
    if (!status?.connected) return;
    const start = new Date(`${date}T00:00:00`), end = new Date(start);
    if (!Number.isFinite(start.getTime())) return;
    end.setDate(end.getDate()+days);
    const controller = new AbortController();
    setLoading(true); setError(""); setEvents([]);
    calendarEvents(start.toISOString(),end.toISOString(),refresh > 0,controller.signal)
      .then(data => { if (!controller.signal.aborted) { setEvents(data.events); setTruncated(data.truncated); } })
      .catch((err: Error & { status?: number }) => { if (!controller.signal.aborted) { setError(err.message); if (err.status === 401) setStatus(old => ({ ...old, enabled:old?.enabled ?? true, connected:false, connecting:false, error:undefined })); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [status?.connected, date, days, refresh]);

  function clearSelection() { detailRequest.current?.abort(); setSelecting(null); onPending?.(false); select.current(null); }
  async function disconnect() {
    loginRequest.current?.abort(); loginRequest.current = null;
    clearSelection(); setConnecting(false); setError("");
    setStatus(old => old ? {...old, connecting:false} : old);
    try { await disconnectCalendar(); setStatus(old => ({ ...old, enabled:old?.enabled ?? true, connected:false, connecting:false, error:undefined })); setEvents([]); }
    catch (err) { setError(err instanceof Error ? err.message : "Не удалось отключить календарь"); }
  }
  async function connect() {
    loginRequest.current?.abort();
    const controller = new AbortController(); loginRequest.current = controller;
    setConnecting(true); setError("");
    const secret = password; setPassword("");
    try {
      const next = await connectCalendar(status?.ldap_bound ? "" : username.trim(),secret,controller.signal);
      if (!controller.signal.aborted) { setStatus(next); if (!next.connected) setError("Подключение не установлено. Проверьте настройки календаря."); }
    } catch (err) { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Не удалось подключиться"); }
    finally { if (!controller.signal.aborted) { setConnecting(false); loginRequest.current = null; } }
  }
  async function choose(event: CalendarEvent) {
    detailRequest.current?.abort();
    const controller = new AbortController(); detailRequest.current = controller;
    select.current(null); onPending?.(true);
    setSelecting(event.id); setError("");
    try { const full = await calendarEvent(event.id,controller.signal); if (!controller.signal.aborted) select.current(full); }
    catch (err) { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Не удалось загрузить событие"); }
    finally { if (!controller.signal.aborted) { setSelecting(null); onPending?.(false); } }
  }
  function move(offset: number) {
    const value = new Date(`${date}T12:00:00`); value.setDate(value.getDate()+offset);
    clearSelection(); setDate(dateValue(value));
  }
  function goToDay(offset: number) {
    const value = new Date(); value.setDate(value.getDate()+offset);
    clearSelection(); setDays(1); setDate(dateValue(value));
  }

  return <aside className="calendar-panel" aria-label="Ваш рабочий календарь">
    <h2>Ваш рабочий календарь</h2>
    <p className="progress-hint">Выберите событие, затем загрузите запись. Название, повестка и приглашённые помогут подготовить протокол.</p>
    {(error || status?.error) && <div className="banner" role="alert">{error || status?.error}</div>}
    {!status && !error && <p role="status">Проверяю подключение…</p>}
    {!status && error && <button className="ghost" onClick={() => { setError(""); void calendarStatus().then(setStatus).catch(err => setError(err.message)); }}>Повторить</button>}
    {status && !status.enabled && <div className="calendar-empty"><p>Администратор ещё не настроил подключение календаря.</p>
      {onOpenSettings && <button className="primary" onClick={onOpenSettings}>Настроить календарь</button>}</div>}
    {status?.enabled && status.connecting && <div>
      <p className="calendar-loading" role="status"><span className="calendar-spinner" />Подключаю ваш календарь…</p>
      <p className="progress-hint">Учётная запись: {status.account_email || userEmail}. Можно продолжать работу в MoM.</p>
      <button className="ghost" type="button" onClick={() => void disconnect()}>Отменить</button>
    </div>}
    {status?.enabled && !status.connected && !status.connecting && <form className="field-stack" onSubmit={event => { event.preventDefault(); void connect(); }}>
      {status.ldap_bound ? <div><p>Календарь учётной записи: <strong>{status.account_email || userEmail}</strong></p>
        <p className="progress-hint">Календарь подключается автоматически при входе с корпоративной учётной записью. Для повторного подключения войдите в MoM заново или введите пароль своей учётной записи ниже.</p>
      </div> : <label className="field-label">Доменный логин или email<input className="title-input" value={username} autoComplete="username" disabled={connecting || disabled}
        placeholder="DOMAIN\login или email@company.ru" onChange={event => setUsername(event.target.value)} /></label>}
      <label className="field-label">Пароль корпоративной учётной записи<input className="title-input" type="password" value={password} autoComplete="current-password" disabled={connecting || disabled}
        onChange={event => setPassword(event.target.value)} /></label>
      <p className="progress-hint">Подключается только ваш календарь. Пароль не сохраняется на диск; после выхода из профиля или перезапуска MoM подключитесь снова.</p>
      <div className="calendar-actions"><button className="primary" disabled={connecting || disabled || !username.trim() || !password}>
        {connecting ? "Подключаю календарь…" : "Подключить календарь"}</button>
        {connecting && <button className="ghost" type="button" onClick={() => void disconnect()}>Отменить</button>}</div>
      {connecting && <p className="calendar-loading" role="status"><span className="calendar-spinner" />Проверяю вход и доступ к календарю…</p>}
    </form>}
    {status?.connected && <>
      <div className="calendar-connected"><span>Календарь · {status.mailbox || status.account_email || status.username}</span><button className="ghost" disabled={disabled} onClick={() => void disconnect()}>Отключить</button></div>
      <div className="calendar-controls">
        <label>Начиная с<input type="date" value={date} disabled={disabled} onChange={event => { if (event.target.value) { clearSelection(); setDate(event.target.value); } }} /></label>
        <label>Период<select value={days} disabled={disabled} onChange={event => { clearSelection(); setDays(Number(event.target.value)); }}><option value={1}>День</option><option value={7}>Неделя</option></select></label>
      </div>
      <div className="calendar-actions"><button className="ghost" disabled={disabled} onClick={() => move(-days)} aria-label="Предыдущий период">←</button>
        <button className="ghost" disabled={disabled} onClick={() => goToDay(-1)}>Вчера</button>
        <button className="ghost" disabled={disabled} onClick={() => goToDay(0)}>Сегодня</button>
        <button className="ghost" disabled={disabled} onClick={() => move(days)} aria-label="Следующий период">→</button>
        <button className="ghost" disabled={loading || disabled} onClick={() => { clearSelection(); setRefresh(n => n+1); }}>Обновить</button></div>
      {loading && <p className="calendar-loading" role="status"><span className="calendar-spinner" />Загружаю события…</p>}
      {!loading && !error && events.length===0 && <p className="calendar-empty">В этом периоде встреч нет. Выберите другую дату или загрузите запись без привязки.</p>}
      {truncated && !loading && <p className="health-warn">Показаны первые 200 событий. Выберите один день, чтобы увидеть нужную встречу.</p>}
      <ol className="calendar-events">{events.map(event => <li key={event.id}>
        <button className={`calendar-event${selectedId===event.id ? " selected" : ""}`} disabled={disabled || event.cancelled} aria-pressed={selectedId===event.id} onClick={() => void choose(event)}>
          <time dateTime={event.start}>{calendarTime(event)}</time><strong>{event.title}</strong>
          {event.location && <small>{event.location}</small>}
          {event.cancelled ? <small>Отменена</small> : selecting===event.id ? <small role="status">Загружаю повестку и приглашённых…</small> : selectedId===event.id ? <small>Выбрана для записи ✓</small> : <small>Выбрать для записи</small>}
        </button></li>)}</ol>
    </>}
  </aside>;
}
