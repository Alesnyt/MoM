import { useEffect, useRef, useState } from "react";
import { listPeople, sendProtocol, type Person } from "./api";
import type { Meeting } from "./types";

type Recipient = { name: string; email: string };
const canonical = (email: string) => email.trim().toLowerCase();
const isEmail = (email: string) => /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/.test(email);

export function ProtocolRecipients({ meeting, revision, ownerEmail, directoryEnabled, onUpdated }: {
  meeting: Meeting; revision: string; ownerEmail?: string; directoryEnabled: boolean;
  onUpdated: (meeting: Meeting) => void;
}) {
  const [selected, setSelected] = useState<Recipient[]>(() => ownerEmail ? [{name: "Вы", email: canonical(ownerEmail)}] : []);
  const [draft, setDraft] = useState<Recipient[] | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const editing = draft !== null;
  const recipients = draft ?? selected;
  const [query, setQuery] = useState("");
  const [manualName, setManualName] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  const [matches, setMatches] = useState<Person[]>([]);
  const [searchNote, setSearchNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const requestId = useRef("");
  const input = query.trim();
  const speakers = (meeting.speakers || []).filter(s => s.id !== "unknown");
  const has = (email: string) => recipients.some(r => r.email === canonical(email));

  useEffect(() => {
    setMatches([]); setSearchNote("");
    if (!directoryEnabled || !editing || input.length < 2 || input.length > 200 || input.includes("@")) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setSearchNote("Ищу в каталоге…");
      try {
        const result = await listPeople(input, controller.signal);
        if (controller.signal.aborted) return;
        const people = result.people.filter(p => p.email);
        setMatches(people);
        setSearchNote(result.error || (people.length ? "" : "Сотрудники с email не найдены."));
      } catch {
        if (!controller.signal.aborted) setSearchNote("Каталог недоступен. Можно ввести email вручную.");
      }
    }, 250);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [directoryEnabled, editing, input]);

  function add(person: Recipient) {
    const email = canonical(person.email);
    setDraft(current => current && !current.some(r => r.email === email)
      ? [...current, {name: person.name, email}] : current);
    setQuery("");
    setManualName("");
    setError("");
  }

  function finishEditing(save: boolean) {
    if (save && draft) {
      if (draft.map(r => r.email).join(",") !== selected.map(r => r.email).join(",")) requestId.current = "";
      setSelected(draft);
    }
    setDraft(null);
    setQuery("");
    setManualName("");
    setError("");
  }

  async function confirm() {
    if (sending.current || meeting.mail_pending || submitted || editing || !selected.length) return;
    sending.current = true; setBusy(true); setError("");
    try {
      // getRandomValues also works on corporate HTTP installations.
      if (!requestId.current) requestId.current = Array.from(crypto.getRandomValues(new Uint8Array(16)),
        byte => byte.toString(16).padStart(2, "0")).join("");
      const next = await sendProtocol(meeting.id, revision, selected.map(r => r.email), requestId.current);
      setSubmitted(true);
      onUpdated(next);
      requestId.current = "";
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось поставить письмо в очередь.");
    } finally {
      sending.current = false; setBusy(false);
    }
  }

  return <section className="protocol-recipients" aria-label="Отправка протокола по почте">
    <h2>Отправка протокола по почте</h2>
    {meeting.mail_pending ? <p role="status">Письмо ожидает завершения доставки.</p> : submitted ?
      <button className="ghost" type="button" onClick={() => setSubmitted(false)}>Подготовить ещё одно письмо</button> : <>
      <p className="delivery-attachment">Вложение — сохранённый протокол в Word</p>
      <div className="delivery-to">
        <strong>Кому</strong>
        {recipients.length > 0 ? <ul className="delivery-addresses" aria-label="Получатели письма">
          {recipients.map(r => <li key={r.email}>
            <span>{r.name && r.name + " · "}{r.email}</span>
            {editing && <button type="button" className="ghost" aria-label={"Убрать " + r.email} onClick={() => {
              setDraft(current => current && current.filter(item => item.email !== r.email));
            }}>Убрать</button>}
          </li>)}
        </ul> : <span className="delivery-empty">Добавьте хотя бы один адрес.</span>}
      </div>
      {recipients.length > 1 && <p className="progress-hint">Все получатели увидят адреса в поле «Кому».</p>}
      {editing && <div className="delivery-editor">
        {speakers.length > 0 && <div className="recipient-options" aria-label="Спикеры встречи">
          {speakers.map(s => <button key={s.id} type="button" className="ghost" disabled={Boolean(s.email && has(s.email))}
            onClick={() => {
              if (s.email) add({name: s.name, email: s.email});
              else {
                setManualName(s.name); setQuery(directoryEnabled ? s.name : ""); searchInput.current?.focus();
              }
            }}>+ {s.name}{s.email ? " · " + s.email : " · Указать адрес"}</button>)}
        </div>}
        <form className="recipient-search" onSubmit={event => {
          event.preventDefault(); if (isEmail(input)) add({name: manualName, email: input});
        }}>
          <label>{manualName ? "Адрес для " + manualName : directoryEnabled ? "Найти в AD или ввести email" : "Добавить email получателя"}
            <input ref={searchInput} className="title-input" type="text" maxLength={254} value={query}
              placeholder={directoryEnabled ? "ФИО или email" : "name@example.ru"}
              onChange={event => {setQuery(event.target.value); setMatches([]);}} />
          </label>
          {manualName && <button className="ghost" type="button" onClick={() => {
            setManualName(""); setQuery(""); searchInput.current?.focus();
          }}>Сбросить выбор спикера</button>}
          <button className="ghost" type="submit" disabled={!isEmail(input)}>Добавить адрес</button>
        </form>
        {searchNote && <p className="progress-hint" role="status">{searchNote}</p>}
        {matches.length > 0 && <ul className="recipient-matches">{matches.map(p =>
          <li key={p.source + p.email}><button type="button" className="ghost" disabled={has(p.email)}
            onClick={() => add(p)}>{p.name} · {p.email}</button></li>)}</ul>}
      </div>}
      {error && <p className="banner" role="alert">{error}</p>}
      <div className="delivery-actions">
        {editing ? <>
          <button className="primary" type="button" disabled={Boolean(input)} onClick={() => finishEditing(true)}>
            Применить список</button>
          <button className="ghost" type="button" onClick={() => finishEditing(false)}>Отмена</button>
        </> : <>
          <button className="ghost" type="button" disabled={busy} onClick={() => {
            setError("");
            setDraft(selected);
          }}>{selected.length ? "Изменить получателей" : "Добавить получателей"}</button>
          <button className="primary" type="button" disabled={busy || !selected.length} onClick={() => void confirm()}>
            {busy ? "Ставлю письмо в очередь…" : "Подтвердить и отправить"}
          </button>
        </>}
      </div>
    </>}
  </section>;
}
