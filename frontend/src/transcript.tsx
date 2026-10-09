import { useEffect, useRef, useState } from "react";
import { assignSpeaker, renameSpeaker } from "./api";
import { formatClock } from "./format";
import type { Meeting, TranscriptSegment } from "./types";

const COLORS = ["#c9843e", "#7f9d6e", "#6e8caf", "#c46b63", "#b089c4", "#d4a017", "#5ec98a", "#e07a5f"];

type Turn = {
  speaker: string;
  start: number;
  indexes: number[];
  text: string;
};

function turnsOf(segments: TranscriptSegment[]): Turn[] {
  const turns: Turn[] = [];
  segments.forEach((segment, index) => {
    const last = turns[turns.length - 1];
    if (last && last.speaker === segment.speaker) {
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

export function TranscriptView({
  meeting,
  onUpdated,
}: {
  meeting: Meeting;
  onUpdated: (meeting: Meeting) => void;
}) {
  const segments = meeting.segments || [];
  const speakers = meeting.speakers || [];
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const saving = useRef(false);

  useEffect(() => {
    setEditing(null);
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
  const turns = turnsOf(segments);

  async function saveName(speakerId: string) {
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      onUpdated(await renameSpeaker(meeting.id, speakerId, draft));
      setEditing(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось переименовать спикера");
    } finally {
      saving.current = false;
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
    <div className="turns">
      <p className="progress-hint">
        Имя меняется у всех реплик этого человека и в протоколе. Если реплика чужая — выберите другого спикера.
        {meeting.diarization_note ? ` ${meeting.diarization_note}` : ""}
      </p>
      {error && <div className="banner" role="alert">{error}</div>}
      {turns.map((turn) => {
        const name = names.get(turn.speaker) || turn.speaker;
        return (
          <article key={`${turn.speaker}-${turn.indexes[0]}`} className="turn" style={{ borderLeftColor: speakerColor(turn.speaker) }}>
            <header className="turn-head">
              {editing === turn.speaker ? (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    void saveName(turn.speaker);
                  }}
                >
                  <input
                    className="speaker-edit"
                    value={draft}
                    autoFocus
                    disabled={busy}
                    aria-label="Имя спикера"
                    onChange={(event) => setDraft(event.target.value)}
                    onBlur={() => {
                      if (draft.trim() && draft.trim() !== name) void saveName(turn.speaker);
                      else setEditing(null);
                    }}
                  />
                </form>
              ) : (
                <button
                  type="button"
                  className="speaker-name"
                  disabled={busy}
                  onClick={() => {
                    setEditing(turn.speaker);
                    setDraft(name);
                  }}
                >
                  {name}
                </button>
              )}
              <time>{formatClock(turn.start)}</time>
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
  );
}
