import { useEffect, useId, useMemo, useRef, useState } from "react";
import { assignSpeaker, listPeople, renameSpeaker, type Person } from "./api";
import { formatClock } from "./format";
import type { Meeting, Speaker, TranscriptSegment } from "./types";

const COLORS = ["#c9843e", "#7f9d6e", "#6e8caf", "#c46b63", "#b089c4", "#d4a017", "#5ec98a", "#e07a5f"];

type Turn = {
  speaker: string;
  start: number | null;
  indexes: number[];
  text: string;
};

function turnsOf(segments: TranscriptSegment[]): Turn[] {
  const turns: Turn[] = [];
  segments.forEach((segment, index) => {
    const last = turns[turns.length - 1];
    if (last && last.speaker === segment.speaker && last.indexes.length < 500) {
      last.indexes.push(index);
      last.text = `${last.text} ${segment.text}`.trim();
      return;
    }
    turns.push({
      speaker: segment.speaker,
      start: segment.start,
      indexes: [index],
      text: segment.text,
    });
  });
  return turns;
}

function speakerColor(id: string): string {
  const number = Number(id.replace(/\D/g, "")) || 1;
  return COLORS[(number - 1) % COLORS.length];
}

function distinctName(desired: string, email: string | undefined, taken: string[]): string {
  const used = new Set(taken.map((item) => item.trim().toLowerCase()));
  const base = desired.trim();
  if (!used.has(base.toLowerCase())) return base;
  if (email) {
    const withMail = `${base} (${email})`;
    if (!used.has(withMail.toLowerCase())) return withMail;
  }
  let number = 2;
  while (used.has(`${base} (${number})`.toLowerCase())) number += 1;
  return `${base} (${number})`;
}

function SpeakerNameEditor({
  speaker,
  taken,
  disabled,
  directoryEnabled,
  onRename,
}: {
  speaker: Speaker;
  taken: string[];
  disabled: boolean;
  directoryEnabled: boolean;
  onRename: (speakerId: string, name: string, email?: string, jobTitle?: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(speaker.name);
  const [open, setOpen] = useState(false);
  const [people, setPeople] = useState<Person[]>([]);
  const [searchState, setSearchState] = useState<"idle" | "loading" | "empty" | "error">("idle");
  const [activeIndex, setActiveIndex] = useState(-1);
  const [directoryError, setDirectoryError] = useState<string | null>(null);
  const listId = useId();
  const inputId = useId();
  const activeOption = useRef<HTMLButtonElement>(null);
  const searchFeedback = useRef<HTMLDivElement>(null);
  const savingRef = useRef(false);
  const defaultName = /^S\d+$/.test(speaker.id) ? `Спикер ${speaker.id.slice(1)}` : null;

  useEffect(() => {
    setDraft(speaker.name);
    setOpen(false);
    setPeople([]);
  }, [speaker.id, speaker.name]);

  useEffect(() => {
    const query = draft.trim();
    if (!open || disabled || query.length < 2 || query === speaker.name.trim()) {
      setPeople([]); setSearchState("idle"); setActiveIndex(-1); setDirectoryError(null);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setPeople([]); setActiveIndex(-1);
    setSearchState("loading");
    setDirectoryError(null);
    const handle = window.setTimeout(() => {
      listPeople(query, controller.signal)
        .then((result) => {
          if (!active) return;
          setPeople(result.people); setActiveIndex(-1); setDirectoryError(result.error || null);
          setSearchState(result.error ? "error" : result.people.length ? "idle" : "empty");
        })
        .catch(() => {
          if (!active) return;
          setPeople([]); setActiveIndex(-1); setSearchState("error");
        });
    }, 250);
    return () => { active = false; controller.abort(); window.clearTimeout(handle); };
  }, [draft, open, speaker.name, disabled]);

  useEffect(() => { activeOption.current?.scrollIntoView({ block: "nearest" }); }, [activeIndex]);
  useEffect(() => {
    if (open && (searchState !== "idle" || people.length)) searchFeedback.current?.scrollIntoView?.({ block: "nearest" });
  }, [open, searchState, people]);

  function closeList() {
    setOpen(false); setPeople([]); setActiveIndex(-1); setSearchState("idle"); setDirectoryError(null);
  }

  function choose(person: Person) {
    setDraft(person.name);
    closeList();
    void save(person.name, person.email, false, person.job_title || "");
  }

  async function save(next = draft, email?: string, reset = false, jobTitle?: string) {
    if (disabled || savingRef.current) return;
    const cleaned = next.trim();
    if (!cleaned || (cleaned === speaker.name.trim() && (email === undefined || email === speaker.email)
        && (jobTitle === undefined || jobTitle === (speaker.job_title || "")))) {
      setDraft(speaker.name);
      closeList();
      return;
    }
    const unique = reset ? cleaned : distinctName(cleaned, email, taken);
    savingRef.current = true;
    try {
      await onRename(speaker.id, unique, reset ? undefined : email, jobTitle);
      if (reset) setDraft(unique);
      closeList();
    } catch {
      /* Ошибку показывает список реплик. */
    } finally {
      savingRef.current = false;
    }
  }

  return (
    <div className="speaker-rename" onBlur={(event) => {
      // Moving focus to Save, Reset, or an option is still inside this editor.
      if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return;
      closeList();
      void save();
    }}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div className="speaker-name-field">
          <label htmlFor={inputId}>{directoryEnabled ? "Найти сотрудника в AD" : "Имя спикера"}</label>
          <div className="speaker-input-wrap">
            <input
              id={inputId}
              className="speaker-edit"
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={open && people.length > 0}
              aria-controls={open && people.length > 0 ? listId : undefined}
              aria-describedby={`${inputId}-hint`}
              aria-busy={open && searchState === "loading"}
              aria-activedescendant={open && activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined}
              autoComplete="off"
              value={draft}
              disabled={disabled}
              aria-label={directoryEnabled ? `Найти сотрудника в AD для ${speaker.name}` : `Имя спикера ${speaker.name}`}
              placeholder={directoryEnabled ? "Начните вводить ФИО" : "Введите имя или email"}
              onFocus={(event) => {
                setOpen(true);
                if (draft === defaultName) event.currentTarget.select();
              }}
              onChange={(event) => {
                setDraft(event.target.value);
                setPeople([]); setActiveIndex(-1);
                setOpen(true);
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  setDraft(speaker.name); closeList();
                } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && people.length) {
                  event.preventDefault(); setOpen(true);
                  setActiveIndex((current) => event.key === "ArrowDown"
                    ? (current + 1) % people.length : (current <= 0 ? people.length - 1 : current - 1));
                } else if (event.key === "Enter" && open && activeIndex >= 0 && people[activeIndex]) {
                  event.preventDefault(); choose(people[activeIndex]);
                }
              }}
            />
            {open && searchState === "loading" && <span className="speaker-input-spinner speaker-search-spinner" aria-hidden="true" />}
          </div>
          <p className="speaker-input-hint" id={`${inputId}-hint`}>
            {directoryEnabled ? "Введите от 2 букв ФИО. Поиск по локальному справочнику AD — выберите сотрудника из списка." : "Выберите сохранённого пользователя или введите имя вручную."}
          </p>
          {open && searchState === "loading" && <div ref={searchFeedback} className="speaker-search-status" role="status">
            <span className="speaker-search-spinner" aria-hidden="true" />
            <span className="speaker-search-label">{directoryEnabled ? "Ищем сотрудника в справочнике AD…" : "Ищем сохранённого пользователя…"}</span>
          </div>}
          {open && (people.length > 0 || searchState === "empty" || searchState === "error") && (
            <div ref={searchFeedback} className="speaker-suggest" onMouseDown={(event) => event.preventDefault()}>
              {searchState === "empty" && <div className="speaker-search-note" role="status">{directoryEnabled ? "В справочнике AD совпадений не найдено." : "Совпадений нет."} Уточните ФИО или сохраните имя вручную.</div>}
              {searchState === "error" && <div className="speaker-search-note" role="status">{directoryError || "Поиск недоступен. Можно сохранить имя вручную."}</div>}
              <ul id={listId} role="listbox" aria-label="Имена из каталога" style={{ listStyle: "none", padding: 0, margin: 0 }}>
                {people.map((person, index) => (
                  <li key={`${person.source}-${person.email || person.name}-${index}`} role="presentation">
                    <button id={`${listId}-${index}`} type="button" role="option" tabIndex={-1}
                      ref={index === activeIndex ? activeOption : undefined}
                      aria-selected={index === activeIndex} disabled={disabled}
                      onMouseEnter={() => setActiveIndex(index)} onClick={() => choose(person)}>
                      <span className="speaker-suggest-name"><strong>{person.name}</strong>
                        <span className="speaker-source">{person.source === "ldap" ? "AD" : "MoM"}</span></span>
                      {person.job_title && <small>{person.job_title}</small>}
                      {person.email && person.email !== person.name ? <small>{person.email}</small> : null}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        <button
          className="ghost"
          type="submit"
          disabled={disabled || draft.trim() === speaker.name.trim()}
          onMouseDown={(event) => event.preventDefault()}
        >
          Сохранить
        </button>
        {defaultName && (speaker.name !== defaultName || draft !== defaultName) && (
          <button
            className="ghost"
            type="button"
            disabled={disabled}
            title={`Вернуть имя «${defaultName}»`}
            aria-label={`Сбросить имя ${speaker.name} на ${defaultName}`}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => void save(defaultName, undefined, true)}
          >
            Сбросить имя
          </button>
        )}
      </form>
    </div>
  );
}

export function TranscriptView({
  meeting,
  onUpdated,
  disabled = false,
  onBusyChange,
  focusSegment,
  directoryEnabled = false,
}: {
  meeting: Meeting;
  onUpdated: (meeting: Meeting) => void;
  disabled?: boolean;
  onBusyChange?: (busy: boolean) => void;
  focusSegment?: number;
  directoryEnabled?: boolean;
}) {
  const segments = meeting.segments || [];
  const speakers = meeting.speakers || [];
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [speakerFilter, setSpeakerFilter] = useState<string | null>(null);
  const [page, setPage] = useState<number | null>(null);
  const speakersHeadingId = useId();
  const transcriptHeadingId = useId();
  const turnsId = useId();

  useEffect(() => {
    setError(null);
    setSpeakerFilter(null);
    setPage(null);
  }, [meeting.id]);

  const turns = useMemo(() => turnsOf(meeting.segments || []), [meeting.segments]);

  if (!segments.length) {
    return (
      <>
        {meeting.diarization_note && <p className="progress-hint">{meeting.diarization_note}</p>}
        {meeting.transcript == null
          ? <p className="progress-hint" role="status">Загружаю транскрипт…</p>
          : <section aria-labelledby={transcriptHeadingId}>
            <h2 id={transcriptHeadingId}>Транскрипт</h2>
            <pre className="transcript">{meeting.transcript || "Транскрипт пуст"}</pre>
          </section>}
      </>
    );
  }

  const namedSpeakers = speakers.filter((item) => item.id !== "unknown" && item.name !== "Неясно");
  const hasUnclear = namedSpeakers.length !== speakers.length;

  function filterBySpeaker(speakerId: string | null) {
    setSpeakerFilter(speakerId === speakerFilter ? null : speakerId);
    setPage(0);
  }

  async function saveName(speakerId: string, name: string, email?: string, jobTitle?: string) {
    setBusy(true);
    onBusyChange?.(true);
    setError(null);
    try {
      onUpdated(await renameSpeaker(meeting.id, speakerId, name, email, jobTitle));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось переименовать спикера");
      throw err;
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  }

  async function moveTurn(indexes: number[], speakerId: string) {
    setBusy(true);
    onBusyChange?.(true);
    setError(null);
    try {
      onUpdated(await assignSpeaker(meeting.id, speakerId, indexes));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сменить спикера");
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  }

  return (
    <div className="speaker-markup">
      {meeting.diarization_note && <p className="speaker-hint">{meeting.diarization_note}</p>}
      {error && <div className="banner" role="alert">{error}</div>}
      {meeting.name_review && <details className="name-review">
        <summary>{meeting.name_review.message}</summary>
        <ul>{meeting.name_review.mentions.map((item, index) => <li key={index}>
          Реплика {item.segment_index + 1}: «{item.original}»
          {item.decision === "correct" ? ` → «${item.replacement}»` :
            item.decision === "confirmed" ? " — имя подтверждено" :
            item.decision === "external" ? " — другой человек, без изменений" : " — неоднозначно, без изменений"}.
          {" "}{item.reason}
        </li>)}</ul>
      </details>}
      <div className="speaker-review-grid">
        <section className="speaker-panel" aria-labelledby={speakersHeadingId}>
          <h2 id={speakersHeadingId}>Назначение спикеров</h2>
          {namedSpeakers.length > 0
            ? <div className="speaker-instructions">
                <p><strong>1. Назначьте имя.</strong> {directoryEnabled ? "Найдите коллегу по ФИО в корпоративном каталоге AD." : "Выберите пользователя или введите имя."}</p>
                <p><strong>2. Проверьте фразы.</strong> Нажмите «Показать реплики» у спикера — транскрипт отфильтруется.</p>
              </div>
            : <p className="speaker-hint">Назначьте спикеров в транскрипте.</p>}
          <div className="speaker-roster">
            {namedSpeakers.map((speaker) => (
              <div className={`speaker-card${speakerFilter === speaker.id ? " speaker-card--selected" : ""}`}
                key={speaker.id} style={{ borderLeftColor: speakerColor(speaker.id) }}>
                <button className="speaker-filter-button" type="button" aria-pressed={speakerFilter === speaker.id}
                  aria-label={`${speakerFilter === speaker.id ? "Сбросить фильтр" : "Показать реплики"}: ${speaker.name}`}
                  aria-controls={turnsId} onClick={() => filterBySpeaker(speaker.id)}>
                  <strong>{/^S\d+$/.test(speaker.id) ? `Спикер ${speaker.id.slice(1)}` : speaker.name}</strong>
                  <span className="speaker-filter-action">{speakerFilter === speaker.id ? "✓ Реплики показаны · Сбросить" : "Показать реплики →"}</span>
                </button>
                <SpeakerNameEditor
                  speaker={speaker}
                  taken={speakers.filter((item) => item.id !== speaker.id).map((item) => item.name)}
                  disabled={busy || disabled}
                  directoryEnabled={directoryEnabled}
                  onRename={saveName}
                />
                {speaker.job_title && <p className="speaker-hint">{speaker.job_title}</p>}
              </div>
            ))}
          </div>
        </section>
        <section className="speaker-panel" aria-labelledby={transcriptHeadingId}>
          <h2 id={transcriptHeadingId}>Транскрипт по спикерам</h2>
          {hasUnclear && <p className="speaker-hint">Уточняйте спикера у каждой реплики с пометкой «Неясно».</p>}
          <TranscriptTurns key={meeting.id} turns={turns} segments={segments} speakers={speakers}
            speakerFilter={speakerFilter} onClearFilter={() => filterBySpeaker(null)} page={page} onPageChange={setPage}
            disabled={busy || disabled} onMove={moveTurn} headingId={transcriptHeadingId} turnsId={turnsId}
            focusSegment={focusSegment} />
        </section>
      </div>
    </div>
  );
}

const TURNS_PER_PAGE = 50;

function TranscriptTurns({
  turns, segments, speakers, speakerFilter, onClearFilter, page, onPageChange,
  disabled, onMove, headingId, turnsId, focusSegment,
}: {
  turns: Turn[];
  segments: TranscriptSegment[];
  speakers: Speaker[];
  speakerFilter: string | null;
  onClearFilter: () => void;
  page: number | null;
  onPageChange: (page: number) => void;
  disabled: boolean;
  onMove: (indexes: number[], speakerId: string) => Promise<void>;
  headingId: string;
  turnsId: string;
  focusSegment?: number;
}) {
  const [query, setQuery] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const focusedTurn = useRef<HTMLElement>(null);
  const names = useMemo(() => new Map(speakers.map((item) => [item.id, item.name])), [speakers]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!speakerFilter && !needle) return turns;
    return turns.filter((turn) => (speakerFilter === null || turn.speaker === speakerFilter) && (!needle ||
      `${names.get(turn.speaker) || turn.speaker} ${turn.text}`.toLocaleLowerCase().includes(needle)));
  }, [turns, names, query, speakerFilter]);
  const requestedPage = page ?? (focusSegment === undefined ? 0
    : Math.max(0, Math.floor(filtered.findIndex((turn) => turn.indexes.includes(focusSegment)) / TURNS_PER_PAGE)));
  const lastPage = Math.max(0, Math.ceil(filtered.length / TURNS_PER_PAGE) - 1);
  const currentPage = Math.min(requestedPage, lastPage);
  const start = currentPage * TURNS_PER_PAGE;
  const visibleTurns = filtered.slice(start, start + TURNS_PER_PAGE);

  useEffect(() => { if (listRef.current) listRef.current.scrollTop = 0; }, [currentPage, query, speakerFilter]);
  useEffect(() => {
    focusedTurn.current?.focus({ preventScroll: true });
    focusedTurn.current?.scrollIntoView?.({ block: "nearest" });
  }, [focusSegment]);

  return <>
    <div className="transcript-controls">
      {speakerFilter !== null && <div className="transcript-filter">
        <span>Показаны только реплики: <strong>{names.get(speakerFilter) || speakerFilter}</strong></span>
        <button type="button" className="ghost" onClick={() => {
          listRef.current?.focus({ preventScroll: true });
          onClearFilter();
        }}>Все спикеры</button>
      </div>}
      <label>Поиск по репликам
        <input type="search" className="title-input" value={query}
          placeholder="Фраза или имя спикера"
          onChange={(event) => { setQuery(event.target.value); onPageChange(0); }} />
      </label>
      <nav className="transcript-pages" aria-label="Страницы транскрипта">
        <button type="button" className="ghost" disabled={currentPage === 0}
          onClick={() => onPageChange(currentPage - 1)}>Предыдущие реплики</button>
        <span role="status">{filtered.length
          ? `Реплики ${start + 1}–${start + visibleTurns.length} из ${filtered.length}`
          : "Реплик не найдено"}</span>
        <button type="button" className="ghost" disabled={currentPage === lastPage}
          onClick={() => onPageChange(currentPage + 1)}>Следующие реплики</button>
      </nav>
    </div>
    <div ref={listRef} id={turnsId} className="turns" role="region" aria-labelledby={headingId} tabIndex={0}>
      {visibleTurns.map((turn) => {
        const name = names.get(turn.speaker) || turn.speaker;
        const focused = focusSegment !== undefined && turn.indexes.includes(focusSegment);
        return (
          <article key={`${turn.speaker}-${turn.indexes[0]}`} className={`turn${focused ? " turn--focused" : ""}`}
            ref={focused ? focusedTurn : undefined} tabIndex={focused ? -1 : undefined}
            style={{ borderLeftColor: speakerColor(turn.speaker) }}>
            <header className="turn-head">
              <strong className="speaker-name">{name}</strong>
              {turn.start !== null && <time>{formatClock(turn.start)}</time>}
              <label className="speaker-move">
                <span className="sr-only">Кто говорит эту реплику</span>
                <select
                  value=""
                  disabled={disabled}
                  aria-label="Кто говорит эту реплику"
                  onChange={(event) => {
                    const next = event.target.value;
                    if (next) void onMove(turn.indexes, next);
                  }}
                >
                  <option value="" disabled>Сменить спикера</option>
                  {speakers.filter((item) => item.id !== turn.speaker).map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                  <option value="new">Новый спикер</option>
                </select>
              </label>
            </header>
            <p>{turn.text}</p>
            {turn.indexes.some((index) => segments[index].context_original_text) &&
              <details><summary>До корпоративного словаря</summary><p className="original-transcript">
                {turn.indexes.map((index) => segments[index].context_original_text ?? segments[index].original_text ?? segments[index].text).join(" ")}
              </p></details>}
            {turn.indexes.some((index) => segments[index].original_text && segments[index].original_text !== segments[index].text) &&
              <details><summary>До проверки имён</summary><p className="original-transcript">
                {turn.indexes.map((index) => segments[index].original_text ?? segments[index].text).join(" ")}
              </p></details>}
          </article>
        );
      })}
    </div>
  </>;
}
