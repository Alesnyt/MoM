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
  const [step, setStep] = useState<"closed" | "choose" | "confirm">("closed");
  const [selected, setSelected] = useState<Recipient[]>(() => ownerEmail ? [{name: "Вы", email: canonical(ownerEmail)}] : []);
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
  const has = (email: string) => selected.some(r => r.email === canonical(email));

  useEffect(() => {
    setMatches([]); setSearchNote("");
    if (!directoryEnabled || step !== "choose" || input.length < 2 || input.length > 200 || input.includes("@")) return;
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
  }, [directoryEnabled, step, input]);

  function add(person: Recipient) {
    const email = canonical(person.email);
    setSelected(current => current.some(r => r.email === email) ? current : [...current, {name: person.name, email}]);
    setQuery(""); setManualName(""); setError(""); requestId.current = "";
  }

  async function confirm() {
    if (sending.current || meeting.mail_pending || !selected.length) return;
    sending.current = true; setBusy(true); setError("");
    try {
      // getRandomValues also works on corporate HTTP installations.
      if (!requestId.current) requestId.current = Array.from(crypto.getRandomValues(new Uint8Array(16)),
        byte => byte.toString(16).padStart(2, "0")).join("");
      const next = await sendProtocol(meeting.id, revision, selected.map(r => r.email), requestId.current);
      onUpdated(next); setStep("closed"); requestId.current = "";
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось поставить письмо в очередь.");
    } finally {
      sending.current = false; setBusy(false);
    }
  }

  if (meeting.mail_pending) return <p className="progress-hint" role="status">Письмо ожидает завершения доставки.</p>;
  if (step === "closed") return <button className="ghost" type="button" onClick={() => {
    setError(""); setStep("choose");
  }}>Отправить протокол по почте</button>;
  return <section className="protocol-recipients" aria-label="Отправка протокола">
    <h3>{step === "choose" ? "Получатели протокола" : "Подтверждение отправки"}</h3>
    {step === "choose" && <>
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
    </>}
    <p>{step === "confirm" ? "Получатели общего письма с сохранённым протоколом в Word" : "Кому"}</p>
    <ul className="delivery-addresses">{selected.map(r => <li key={r.email}>
      <span>{r.name && r.name + " · "}{r.email}</span>
      {step === "choose" && <button type="button" className="ghost" aria-label={"Убрать " + r.email} onClick={() => {
        setSelected(current => current.filter(item => item.email !== r.email)); requestId.current = "";
      }}>Убрать</button>}
    </li>)}</ul>
    {!selected.length && <p className="progress-hint">Добавьте хотя бы один адрес.</p>}
    {selected.length > 1 && <p className="progress-hint">Все получатели увидят адреса в поле «Кому».</p>}
    {error && <p className="banner" role="alert">{error}</p>}
    <div className="recipient-options">
      {step === "choose"
        ? <button className="primary" type="button" disabled={!selected.length || Boolean(input)}
            onClick={() => setStep("confirm")}>Проверить получателей</button>
        : <button className="primary" type="button" disabled={busy} onClick={() => void confirm()}>
            {busy ? "Ставлю письмо в очередь…" : "Подтвердить и отправить"}</button>}
      <button className="ghost" type="button" disabled={busy} onClick={() => {
        setError(""); setStep(step === "confirm" ? "choose" : "closed");
      }}>{step === "confirm" ? "Изменить получателей" : "Отмена"}</button>
    </div>
  </section>;
}
