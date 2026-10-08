import { useEffect, useRef, useState } from "react";
import { downloadMarkdown, getEmailBody, openMeetingEmail } from "./api";
import { count, formatClock, formatDate, formatDuration, priorityLabel } from "./format";
import type { Meeting, MeetingResult, MeetingStatus, Tab } from "./types";

export const STEPS: { id: MeetingStatus; label: string }[] = [
  { id: "extracting", label: "Аудио" },
  { id: "transcribing", label: "Расшифровка" },
  { id: "analyzing", label: "Протокол" },
];

export function MeetingPane({
  meeting,
  tab,
  mailEnabled,
  onTab,
  onRetry,
  onDelete,
}: {
  meeting: Meeting;
  tab: Tab;
  mailEnabled: boolean;
  onTab: (tab: Tab) => void;
  onRetry: () => void;
  onDelete: () => void;
}) {
  const processing = meeting.status !== "done" && meeting.status !== "error";
  const result = meeting.result;
  const [mailNote, setMailNote] = useState<string | null>(null);

  async function sendByEmail() {
    setMailNote(null);
    const mode = await openMeetingEmail(meeting);
    if (mode === "opened-outlook") {
      setMailNote("Черновик открыт в Outlook. Текст письма также скопирован в буфер.");
    } else if (mode === "opened-mail") {
      setMailNote(
        "Открыто в Почте Apple — Outlook на Mac блокирует скачанные .eml. Текст скопирован: Новое письмо → вставить.",
      );
    } else if (mode === "opened" || mode === "saved") {
      setMailNote("Текст письма скопирован в буфер. Вставьте его в новое письмо Outlook.");
    }
  }

  async function copyProtocol() {
    setMailNote(null);
    const text =
      tab === "transcript" && meeting.transcript
        ? meeting.transcript
        : await getEmailBody(meeting.id);
    try {
      await navigator.clipboard.writeText(text);
      setMailNote(
        tab === "transcript" ? "Транскрипт скопирован в буфер обмена." : "Протокол скопирован в буфер обмена.",
      );
    } catch {
      setMailNote("Не удалось скопировать. Разрешите сайту доступ к буферу обмена.");
    }
  }

  return (
    <section className="pane">
      <header className="pane-head">
        <div>
          <p className="eyebrow">
            {formatDate(meeting.created_at)}
            {meeting.duration_seconds
              ? ` · ${formatDuration(meeting.duration_seconds)}`
              : ""}
            {meeting.language ? ` · ${meeting.language}` : ""}
          </p>
          <h1>{meeting.title}</h1>
        </div>
        <div className="pane-actions">
          {meeting.status === "done" && meeting.result && (
            <>
              <button
                className="ghost"
                type="button"
                title="Копирует саммари, решения и поручения. На вкладке «Транскрипт» копирует расшифровку."
                onClick={() => void copyProtocol()}
              >
                Копировать
              </button>
              <button
                className="ghost"
                type="button"
                title="Откроет почту. Длинный протокол не скачивается как .eml — Apple иначе блокирует файл."
                onClick={() => void sendByEmail()}
              >
                Отправить письмом
              </button>
              <button
                className="ghost"
                onClick={() => downloadMarkdown(meeting.id, meeting.title)}
              >
                Скачать .md
              </button>
            </>
          )}
          {(meeting.status === "error" || meeting.status === "done") && (
            <button className="ghost" onClick={onRetry}>
              Повторить
            </button>
          )}
          {!processing && (
            <button className="ghost danger" onClick={onDelete}>
              Удалить
            </button>
          )}
        </div>
      </header>

      {mailNote && <div className="banner">{mailNote}</div>}

      {processing && <ProcessCard meeting={meeting} mailEnabled={mailEnabled} />}
      {meeting.status === "error" && (
        <div className="error-card">
          <strong>Обработка не удалась</strong>
          <p>{meeting.error || meeting.status_message}</p>
        </div>
      )}

      {result && meeting.status === "done" && (
        <>
          <nav className="tabs">
            <TabButton id="overview" tab={tab} onTab={onTab} label="Саммари" />
            <TabButton
              id="actions"
              tab={tab}
              onTab={onTab}
              label={`Поручения${count(result.action_items)}`}
            />
            <TabButton id="mom" tab={tab} onTab={onTab} label="Протокол" />
            <TabButton id="transcript" tab={tab} onTab={onTab} label="Транскрипт" />
          </nav>
          {tab === "overview" && <Overview result={result} />}
          {tab === "actions" && <Actions result={result} />}
          {tab === "mom" && <Minutes meeting={meeting} result={result} />}
          {tab === "transcript" && (
            <pre className="transcript">{meeting.transcript || "Транскрипт пуст"}</pre>
          )}
        </>
      )}
    </section>
  );
}

function TabButton({
  id,
  tab,
  onTab,
  label,
}: {
  id: Tab;
  tab: Tab;
  onTab: (tab: Tab) => void;
  label: string;
}) {
  return (
    <button className={tab === id ? "active" : ""} onClick={() => onTab(id)}>
      {label}
    </button>
  );
}

function transcribeHint(message: string | null | undefined): string {
  const text = (message || "").toLowerCase();
  if (text.includes("gigaam") || text.includes("сбер")) {
    return "GigaAM идёт по записи. Длинные файлы режутся на фрагменты, процент растёт по ходу.";
  }
  if (text.includes("whisper")) {
    return "Whisper идёт по записи, процент растёт вместе с таймкодом.";
  }
  return "Расшифровка идёт по записи, процент растёт по ходу.";
}

function ProcessCard({ meeting, mailEnabled }: { meeting: Meeting; mailEnabled: boolean }) {
  const current = STEPS.findIndex((step) => step.id === meeting.status);
  const index = meeting.status === "queued" ? -1 : current;
  const queued = meeting.status === "queued";
  const pct = Math.max(0, Math.min(100, meeting.progress ?? fallbackProgress(meeting.status)));
  const stamp = `${meeting.status}|${pct}|${meeting.status_message}`;
  const started = useRef({ stamp, at: Date.now() });
  if (started.current.stamp !== stamp) {
    started.current = { stamp, at: Date.now() };
  }
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [meeting.id]);
  const elapsed = Math.max(0, Math.floor((now - started.current.at) / 1000));
  const waiting = meeting.status === "analyzing" || elapsed >= 4;
  const hint = queued
    ? mailEnabled
      ? "Сервер обрабатывает встречи в очереди. Когда дойдёт ваша — начнётся расшифровка, готовый протокол придёт на почту."
      : "Сервер обрабатывает встречи в очереди. Когда дойдёт ваша — начнётся расшифровка. Результат появится в MoM."
    : meeting.status === "analyzing"
      ? "Qwen пишет протокол. Процент обновится, когда модель ответит — для длинной записи это несколько минут."
      : meeting.status === "transcribing"
        ? transcribeHint(meeting.status_message)
        : "Обработка идёт.";
  return (
    <div className="process">
      <div className="progress-head">
        <strong>{queued ? "очередь" : `${pct}%`}</strong>
        <span>{meeting.status_message || stepLabel(meeting.status)}</span>
      </div>
      <div
        className={`progress-bar${waiting ? " waiting" : ""}`}
        role="progressbar"
        aria-valuenow={queued ? 0 : pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <i style={{ width: `${queued ? 6 : pct}%` }} />
      </div>
      <p className="progress-live">
        {queued ? `Ожидание ${formatClock(elapsed)}` : `В работе ${formatClock(elapsed)}`}
        {!queued && waiting ? " · процесс живой, ждём ответ" : ""}
      </p>
      <ol>
        {STEPS.map((step, i) => (
          <li
            key={step.id}
            className={i < index ? "done" : i === index ? "now" : ""}
          >
            <span>{String(i + 1).padStart(2, "0")}</span>
            {step.label}
            {i === index ? " · идёт" : i < index ? " · готово" : ""}
          </li>
        ))}
      </ol>
      <p className="progress-hint">{hint}</p>
    </div>
  );
}

export function stepLabel(status: MeetingStatus): string {
  if (status === "queued") return "В очереди";
  const step = STEPS.find((item) => item.id === status);
  return step?.label || status;
}

function fallbackProgress(status: MeetingStatus): number {
  if (status === "queued") return 2;
  if (status === "extracting") return 8;
  if (status === "transcribing") return 20;
  if (status === "analyzing") return 88;
  if (status === "done") return 100;
  return 0;
}

function Overview({ result }: { result: MeetingResult }) {
  return (
    <div className="overview">
      {result.participants.length > 0 && (
        <p className="people">Участники: {result.participants.join(", ")}</p>
      )}
      <article className="summary">
        {result.summary.split("\n").filter(Boolean).map((para, index) => (
          <p key={index}>{para}</p>
        ))}
      </article>
      <div className="split">
        {result.key_points.length > 0 && (
          <section>
            <h2>Тезисы</h2>
            <ul>
              {result.key_points.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </section>
        )}
        {result.decisions.length > 0 && (
          <section>
            <h2>Решения</h2>
            <ul className="decisions">
              {result.decisions.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}

function Actions({ result }: { result: MeetingResult }) {
  if (result.action_items.length === 0) {
    return <p className="empty-note">В записи не нашлось явных поручений.</p>;
  }
  return (
    <table className="actions">
      <thead>
        <tr>
          <th>Поручение</th>
          <th>Исполнитель</th>
          <th>Срок</th>
          <th>Приоритет</th>
        </tr>
      </thead>
      <tbody>
        {result.action_items.map((item, index) => (
          <tr key={`${item.task}-${index}`}>
            <td>{item.task}</td>
            <td>{item.assignee || "—"}</td>
            <td>{item.due || "—"}</td>
            <td>
              <span className={`prio ${item.priority}`}>{priorityLabel(item.priority)}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Minutes({ meeting, result }: { meeting: Meeting; result: MeetingResult }) {
  return (
    <article className="paper">
      <header>
        <p>Minutes of Meeting</p>
        <h2>{result.title || meeting.title}</h2>
        <ul>
          <li>Обработано: {formatDate(meeting.created_at)}</li>
          {result.date_hint && <li>По обсуждению: {result.date_hint}</li>}
          {meeting.duration_seconds && (
            <li>Длительность: {formatDuration(meeting.duration_seconds)}</li>
          )}
          {result.participants.length > 0 && (
            <li>Участники: {result.participants.join(", ")}</li>
          )}
        </ul>
      </header>
      {(result.mom.agenda || []).map((block, index) => (
        <section key={index}>
          <h3>{block.topic}</h3>
          {block.discussion && <p>{block.discussion}</p>}
          {block.outcome && (
            <p className="outcome">
              <span>Итог.</span> {block.outcome}
            </p>
          )}
        </section>
      ))}
      {result.mom.next_meeting && (
        <p className="next">Следующая встреча: {result.mom.next_meeting}</p>
      )}
    </article>
  );
}
