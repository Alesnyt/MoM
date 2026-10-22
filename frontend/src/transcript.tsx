import { useEffect, useRef, useState } from "react";
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
  onRename,
}: {
  speaker: Speaker;
  taken: string[];
  disabled: boolean;
  onRename: (speakerId: string, name: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(speaker.name);
  const [open, setOpen] = useState(false);
  const [people, setPeople] = useState<Person[]>([]);
  const cancelRef = useRef(false);
  const savingRef = useRef(false);
  const resetRef = useRef<HTMLButtonElement>(null);
  const defaultName = /^S\d+$/.test(speaker.id) ? `Спикер ${speaker.id.slice(1)}` : null;

  useEffect(() => {
    setDraft(speaker.name);
    setOpen(false);
    setPeople([]);
  }, [speaker.id, speaker.name]);

  useEffect(() => {
    const query = draft.trim();
    if (!open || query.length < 2 || query === speaker.name.trim()) {
      setPeople([]);
      return;
    }
    const handle = window.setTimeout(() => {
      listPeople(query)
        .then(setPeople)
        .catch(() => setPeople([]));
    }, 250);
    return () => window.clearTimeout(handle);
  }, [draft, open, speaker.name]);

  function closeList() {
    setOpen(false);
    setPeople([]);
  }

  async function save(next = draft, email?: string, reset = false) {
    if (disabled || savingRef.current) return;
    const cleaned = next.trim();
    if (!cleaned || cleaned === speaker.name.trim()) {
      setDraft(speaker.name);
      closeList();
      return;
    }
    const unique = reset ? cleaned : distinctName(cleaned, email, taken);
    savingRef.current = true;
    try {
      await onRename(speaker.id, unique);
      if (reset) setDraft(unique);
      closeList();
    } catch {
      /* Ошибку показывает список реплик. */
    } finally {
      savingRef.current = false;
    }
  }

  return (
    <div className="speaker-rename">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <input
          className="speaker-edit"
          value={draft}
          disabled={disabled}
          aria-label={`Имя спикера ${speaker.name}`}
          placeholder="Имя из каталога или своё"
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            setDraft(event.target.value);
            setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              cancelRef.current = true;
              setDraft(speaker.name);
              closeList();
              event.currentTarget.blur();
            }
          }}
          onBlur={(event) => {
            // Tabbing to reset must not save the draft before resetting it.
            if (resetRef.current && event.relatedTarget === resetRef.current) return;
            if (cancelRef.current) {
              cancelRef.current = false;
              return;
            }
            void save();
          }}
        />
        <button
          className="ghost"
          type="submit"
          disabled={disabled || draft.trim() === speaker.name.trim()}
          onMouseDown={(event) => event.preventDefault()}
        >
          Сохранить
        </button>
        {defaultName && (
          <button
            ref={resetRef}
            className="ghost"
            type="button"
            disabled={disabled || (speaker.name === defaultName && draft === defaultName)}
            title={`Вернуть имя «${defaultName}»`}
            aria-label={`Сбросить имя ${speaker.name} на ${defaultName}`}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => void save(defaultName, undefined, true)}
          >
            Сбросить имя
          </button>
        )}
      </form>
      {open && people.length > 0 && (
        <ul className="speaker-suggest">
          {people.map((person) => (
            <li key={`${person.source}-${person.email || person.name}`}>
              <button
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  setDraft(person.name);
                  closeList();
                  void save(person.name, person.email);
                }}
              >
                <strong>{person.name}</strong>
                {person.email && person.email !== person.name ? <small>{person.email}</small> : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function TranscriptView({
  meeting,
  onUpdated,
}: {
  meeting: Meeting;
  onUpdated: (meeting: Meeting) => void;
}) {
  const segments = meeting.segments || [];
  const speakers = meeting.speakers || [];
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
  }, [meeting.id]);

  if (!segments.length) {
    return (
      <>
        {meeting.diarization_note && <p className="progress-hint">{meeting.diarization_note}</p>}
        <pre className="transcript">{meeting.transcript || "Транскрипт пуст"}</pre>
      </>
    );
  }

  const names = new Map(speakers.map((item) => [item.id, item.name]));
  const namedSpeakers = speakers.filter((item) => item.id !== "unknown" && item.name !== "Неясно");
  const hasUnclear = namedSpeakers.length !== speakers.length;
  const turns = turnsOf(segments);

  async function saveName(speakerId: string, name: string) {
    setBusy(true);
    setError(null);
    try {
      onUpdated(await renameSpeaker(meeting.id, speakerId, name));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось переименовать спикера");
      throw err;
    } finally {
      setBusy(false);
    }
  }

  async function moveTurn(indexes: number[], speakerId: string) {
    setBusy(true);
    setError(null);
    try {
      onUpdated(await assignSpeaker(meeting.id, speakerId, indexes));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сменить спикера");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="speaker-markup">
      <p className="progress-hint">
        Имя можно выбрать из каталога или вписать своё. Оно встанет на все реплики этого спикера и появится в списке выбора.
        {" «Сбросить имя» вернёт обозначение «Спикер 1», «Спикер 2» и т. д."}
        {hasUnclear ? " «Неясно» — это не один человек: такие реплики назначайте по одной в списке справа." : ""}
        {meeting.diarization_note ? ` ${meeting.diarization_note}` : ""}
      </p>
      {error && <div className="banner" role="alert">{error}</div>}
      <div className="speaker-roster">
        {namedSpeakers.map((speaker) => (
          <SpeakerNameEditor
            key={speaker.id}
            speaker={speaker}
            taken={speakers.filter((item) => item.id !== speaker.id).map((item) => item.name)}
            disabled={busy}
            onRename={saveName}
          />
        ))}
      </div>
      <div className="turns">
      {turns.map((turn) => {
        const name = names.get(turn.speaker) || turn.speaker;
        return (
          <article key={`${turn.speaker}-${turn.indexes[0]}`} className="turn" style={{ borderLeftColor: speakerColor(turn.speaker) }}>
            <header className="turn-head">
              <strong className="speaker-name">{name}</strong>
              {turn.start !== null && <time>{formatClock(turn.start)}</time>}
              <label className="speaker-move">
                <span className="sr-only">Кто говорит эту реплику</span>
                <select
                  value={turn.speaker}
                  disabled={busy}
                  aria-label="Кто говорит эту реплику"
                  onChange={(event) => {
                    const next = event.target.value;
                    if (next !== turn.speaker) void moveTurn(turn.indexes, next);
                  }}
                >
                  {speakers.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                  <option value="new">Новый спикер</option>
                </select>
              </label>
            </header>
            <p>{turn.text}</p>
          </article>
        );
      })}
      </div>
    </div>
  );
}
