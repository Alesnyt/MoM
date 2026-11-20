import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { apiRequest } from "./http";
import { priorityLabel } from "./format";
import type { ActionItem, AgendaItem, Meeting, MeetingResult } from "./types";

const SummaryEditor = lazy(() => import("./summary-editor").then(module => ({ default: module.SummaryEditor })));

type Suggestion = { field: string; before: string; after: string; reason: string };
type Props = {
  meetingId: string; initial: MeetingResult; revision: string; summaryOnly?: boolean;
  onSaved: (meeting: Meeting) => void; onCancel: () => void; onDirtyChange?: (dirty: boolean) => void;
};
const lists = [
  ["participants", "Участники", "Участник"],
  ["decisions", "Решения", "Решение"],
  ["key_points", "Контекст и открытые вопросы", "Тезис"],
] as const;

function replaceAt<T>(values: T[], index: number, updated: T) {
  return values.map((item, i) => i === index ? updated : item);
}

export function ProtocolEditor({ meetingId, initial, revision, summaryOnly = false, onSaved, onCancel, onDirtyChange }: Props) {
  const [draft, setDraft] = useState(initial);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"check" | "save" | null>(null);
  const request = useRef<AbortController | null>(null);
  const dirty = draft !== initial && JSON.stringify(draft) !== JSON.stringify(initial);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    onDirtyChange?.(dirty);
    if (!dirty) return;
    function warn(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = ""; }
    window.addEventListener("beforeunload", warn);
    return () => { window.removeEventListener("beforeunload", warn); onDirtyChange?.(false); };
  }, [dirty, onDirtyChange]);

  function change(next: MeetingResult, invalidate: string) {
    setDraft(next);
    setSuggestions(old => old.filter(item => item.field !== invalidate && !item.field.startsWith(invalidate + ".")));
    setNote("");
    setError("");
  }

  async function submit(mode: "check" | "save") {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(mode);
    setNote("");
    setError("");
    try {
      const path = "/api/meetings/" + encodeURIComponent(meetingId) + "/protocol";
      const response = await apiRequest(mode === "check" ? path + "/proofread" : path, {
        method: mode === "check" ? "POST" : "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(summaryOnly ? { revision, summary: draft.summary, summary_format: draft.summary_format } : { revision, result: draft }),
        signal: controller.signal,
      });
      const data = await response.json();
      if (controller.signal.aborted) return;
      if (mode === "save") onSaved(data);
      else {
        setSuggestions(data.suggestions);
        setNote(data.suggestions.length ? "Предложений к проверке: " + data.suggestions.length : "Предложений к исправлению нет.");
      }
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Не удалось выполнить запрос.");
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (!controller.signal.aborted) setBusy(null);
      }
    }
  }

  function textField(path: string, label: string, value: string | null, update: (text: string) => MeetingResult,
                     options: { rows?: number; required?: boolean } = {}) {
    const text = value ?? "";
    const props = { className: "title-input", value: text, required: options.required, lang: "ru",
      onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => change(update(e.target.value), path) };
    return <div className="editor-field" key={path}>
      {path === "summary" ? <>
        {!summaryOnly && <h3>Саммари</h3>}
        <Suspense fallback={<p role="status">Открываю редактор…</p>}>
          <SummaryEditor value={text} format={draft.summary_format ?? "plain"} disabled={busy !== null} autoFocus={summaryOnly}
            onChange={summary => change({ ...draft, summary, summary_format: "markdown" }, path)} />
        </Suspense>
      </> : <label className="field-label">{label}
        {options.rows ? <textarea {...props} rows={options.rows} /> : <input {...props} />}
      </label>}
      {suggestions.filter(s => s.field === path).map((s, index) =>
        <div className="proof-suggestion" key={index}>
          <div><del>{s.before}</del> → <ins>{s.after}</ins></div><p>{s.reason}</p>
          <button type="button" className="ghost" onClick={() => {
            const offset = text.indexOf(s.before);
            if (offset < 0 || offset !== text.lastIndexOf(s.before)) return;
            const next = text.slice(0, offset) + s.after + text.slice(offset + s.before.length);
            setDraft(update(next));
            setNote("");
            setSuggestions(old => old.filter(item => item !== s && (item.field !== path ||
              (next.includes(item.before) && next.indexOf(item.before) === next.lastIndexOf(item.before)))));
          }}>Принять</button>{" "}
          <button type="button" className="ghost" onClick={() => {
            setSuggestions(old => old.filter(item => item !== s)); setNote("");
          }}>Пропустить</button>
        </div>)}
    </div>;
  }

  const summaryField = textField("summary", "Саммари", draft.summary, summary => ({ ...draft, summary }));
  return <form className="protocol-editor" aria-label={summaryOnly ? "Редактор саммари" : "Редактор протокола"} onSubmit={e => {
    e.preventDefault(); void submit("save");
  }}>
    <h2>{summaryOnly ? "Редактирование саммари" : "Редактирование протокола"}</h2>
    {summaryOnly && <fieldset className="editor-body" disabled={busy !== null}>{summaryField}</fieldset>}
    <div className="editor-tools">
      <button type="submit" className="primary" disabled={!dirty || busy !== null}>
        {busy === "save" ? "Сохраняю…" : summaryOnly ? "Сохранить саммари" : "Сохранить протокол"}
      </button>
      <button type="button" className="ghost" disabled={busy !== null} onClick={() => void submit("check")}>
        {busy === "check" ? "Проверяю русский язык…" : "Проверить русский язык"}
      </button>
      <button type="button" className="ghost" disabled={busy === "save"} onClick={() => {
        if (!dirty || window.confirm("Закрыть редактор и отбросить несохранённые правки?")) onCancel();
      }}>Отмена</button>
    </div>
    {note && <p role="status">{note}</p>}
    {error && <p className="banner" role="alert">{error}</p>}
    {!summaryOnly && <fieldset className="editor-body" disabled={busy !== null}>
      {textField("title", "Название встречи", draft.title, title => ({ ...draft, title }), { required: true })}
      {textField("date_hint", "Дата встречи", draft.date_hint, date => ({ ...draft, date_hint: date || null }))}
      {summaryField}
      {lists.map(([key, title, label]) => <section key={key}><h3>{title}</h3>
        {draft[key].map((value, i) => <div key={i} className="editor-row">
          {textField(key + "." + i, label + " " + (i + 1), value,
            text => ({ ...draft, [key]: replaceAt(draft[key], i, text) }), { required: true })}
          <button type="button" className="ghost" aria-label={"Убрать: " + label + " " + (i + 1)}
            onClick={() => change({ ...draft, [key]: draft[key].filter((_, j) => j !== i) }, key)}>Убрать</button>
        </div>)}
        <button type="button" className="ghost" onClick={() => change({ ...draft, [key]: [...draft[key], ""] }, key)}>
          {key === "participants" ? "Добавить участника" : key === "decisions" ? "Добавить решение" : "Добавить тезис"}
        </button>
      </section>)}
      <section><h3>Поручения</h3>
        {draft.action_items.map((item, i) => {
          const update = (patch: Partial<ActionItem>) => ({ ...draft, action_items: replaceAt(draft.action_items, i, { ...item, ...patch }) });
          const path = "action_items." + i + ".";
          return <fieldset key={i} className="editor-group"><legend>Поручение {i + 1}</legend>
            {textField(path + "task", "Задача", item.task, task => update({ task }), { rows: 2, required: true })}
            {textField(path + "assignee", "Исполнитель", item.assignee, v => update({ assignee: v || null }))}
            {textField(path + "due", "Срок", item.due, v => update({ due: v || null }))}
            <label className="field-label">Приоритет<select className="title-input" value={item.priority}
              onChange={e => change(update({ priority: e.target.value as ActionItem["priority"] }), path + "priority")}>
              {["low", "medium", "high"].map(p => <option key={p} value={p}>{priorityLabel(p)}</option>)}
            </select></label>
            <button type="button" className="ghost" onClick={() => change({ ...draft,
              action_items: draft.action_items.filter((_, j) => i !== j) }, "action_items")}>Убрать поручение</button>
          </fieldset>;
        })}
        <button type="button" className="ghost" onClick={() => change({ ...draft,
          action_items: [...draft.action_items, { task: "", assignee: null, due: null, priority: "medium" }] }, "action_items")}>
          Добавить поручение</button>
      </section>
      <section><h3>Повестка</h3>
        {draft.mom.agenda.map((item, i) => {
          const update = (patch: Partial<AgendaItem>) => ({ ...draft, mom: { ...draft.mom,
            agenda: replaceAt(draft.mom.agenda, i, { ...item, ...patch }) } });
          const path = "mom.agenda." + i + ".";
          return <fieldset key={i} className="editor-group"><legend>Пункт {i + 1}</legend>
            {textField(path + "topic", "Тема", item.topic, topic => update({ topic }))}
            {textField(path + "discussion", "Обсуждение", item.discussion, discussion => update({ discussion }), { rows: 2 })}
            {textField(path + "outcome", "Итог", item.outcome, outcome => update({ outcome }), { rows: 2 })}
            <button type="button" className="ghost" onClick={() => change({ ...draft, mom: { ...draft.mom,
              agenda: draft.mom.agenda.filter((_, j) => i !== j) } }, "mom.agenda")}>Убрать пункт</button>
          </fieldset>;
        })}
        <button type="button" className="ghost" onClick={() => change({ ...draft, mom: { ...draft.mom,
          agenda: [...draft.mom.agenda, { topic: "", discussion: "", outcome: "" }] } }, "mom.agenda")}>Добавить пункт</button>
      </section>
      {textField("mom.next_meeting", "Следующая встреча", draft.mom.next_meeting,
        v => ({ ...draft, mom: { ...draft.mom, next_meeting: v || null } }))}
    </fieldset>}
  </form>;
}
