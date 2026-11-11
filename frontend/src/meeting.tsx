import { useEffect, useRef, useState } from "react";
import { downloadMeetingEmail, downloadProtocol, getEmailBody, startProtocol } from "./api";
import { copyForWord } from "./copy";
import { count, formatClock, formatDate, formatDuration, priorityLabel, queuePositionLabel } from "./format";
import { isProtocolResult } from "./protocol";
import { TranscriptView } from "./transcript";
import { ProtocolRecipients } from "./recipients";
import type { Meeting, MeetingResult, MeetingStatus, ProtocolRecipient, Tab } from "./types";

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
  onStop,
  onRestart,
  onDelete,
  onUpdated,
}: {
  meeting: Meeting;
  tab: Tab;
  mailEnabled: boolean;
  onTab: (tab: Tab) => void;
  onRetry: () => void;
  onStop: () => void;
  onRestart: () => void;
  onDelete: () => void;
  onUpdated: (meeting: Meeting) => void;
}) {
  const processing = meeting.status !== "done" && meeting.status !== "error" && meeting.status !== "review";
  const rawResult = isProtocolResult(meeting.result) ? meeting.result : null;
  const roles = new Map((meeting.speakers || []).filter((speaker) => speaker.job_title)
    .map((speaker) => [speaker.name.toLocaleLowerCase(), speaker.job_title]));
  const result = rawResult && { ...rawResult, participants: rawResult.participants.map((name) => {
    const role = roles.get(name.toLocaleLowerCase());
    return role ? `${name} — ${role}` : name;
  }) };
  const wide = meeting.status === "review" || (meeting.status === "done" && (tab === "transcript" || !result));
  const [mailNote, setMailNote] = useState<string | null>(null);
  const wordContent = useRef<HTMLElement>(null);
  const [downloading, setDownloading] = useState(false);
  const [focusSegment, setFocusSegment] = useState<number | undefined>();

  function openSource(turn: number) {
    setFocusSegment(turn - 1);
    onTab("transcript");
  }

  async function download(format: "docx" | "md") {
    setMailNote(null);
    setDownloading(true);
    try {
      await downloadProtocol(meeting.id, meeting.title, format);
    } catch (err) {
      setMailNote(err instanceof Error ? err.message : "Не удалось скачать протокол.");
    } finally {
      setDownloading(false);
    }
  }

  async function copyWord() {
    if (!wordContent.current) return;
    try {
      await copyForWord(wordContent.current);
      setMailNote("Протокол скопирован. В Word выберите «Сохранить исходное форматирование».");
    } catch {
      setMailNote("Не удалось скопировать с форматированием. Скачайте протокол в Word (.docx).");
    }
  }

  async function downloadEmail() {
    setMailNote(null);
    setDownloading(true);
    try {
      await downloadMeetingEmail(meeting);
      setMailNote("Письмо скачано. Откройте файл .eml в почтовом клиенте, чтобы переслать его с протоколом Word.");
    } catch (err) {
      setMailNote(err instanceof Error ? err.message : "Не удалось скачать письмо.");
    } finally {
      setDownloading(false);
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
    <section className={wide ? "pane pane--transcript" : "pane"}>
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
          {meeting.status === "done" && result && (
            <>
              <button className="primary" type="button" disabled={downloading} onClick={() => void download("docx")}>
                Скачать протокол в Word (.docx)
              </button>
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
                disabled={downloading}
                onClick={() => void downloadEmail()}
              >
                Скачать письмо с Word
              </button>
              <button
                className="ghost"
                type="button"
                disabled={downloading}
                onClick={() => void download("md")}
              >
                Скачать .md
              </button>
            </>
          )}
          {(meeting.status === "error" || meeting.status === "done") && (
            <button className="ghost" onClick={onRetry}>
              {(meeting.status === "error" || meeting.source_type === "text") && meeting.transcript ? "Собрать протокол" : "Повторить"}
            </button>
          )}
          {!processing && (
            <button className="ghost danger" onClick={onDelete}>
              Удалить
            </button>
          )}
        </div>
      </header>

      {mailNote && <div className="banner" role="status">{mailNote}</div>}

      {processing && (
        <ProcessCard meeting={meeting} mailEnabled={mailEnabled} onStop={onStop} onRestart={onRestart} />
      )}
      {meeting.status === "review" && (
        <SpeakerReview key={meeting.id} meeting={meeting} mailEnabled={mailEnabled} onUpdated={onUpdated} />
      )}
      {meeting.status === "error" && (
        <div className="error-card">
          <strong>Обработка не удалась</strong>
          <p>{meeting.error || meeting.status_message}</p>
        </div>
      )}

      {meeting.status === "done" && !result && (
        <div className="error-card">
          <strong>Не удалось прочитать сохранённый протокол</strong>
          <p>Ответ модели имеет неверный формат. Сформируйте протокол заново.</p>
          <button onClick={onRetry}>Повторить обработку</button>
          <TranscriptView meeting={meeting} onUpdated={onUpdated} />
        </div>
      )}
      {result && meeting.status === "done" && (
        <>
          {meeting.status_message && meeting.status_message !== "Готово" &&
            <p className="progress-hint" role="status">{meeting.status_message}</p>}
          {(meeting.speakers || []).some((speaker) => speaker.send_protocol) && <p className="progress-hint">
            Дополнительные получатели: {(meeting.speakers || [])
              .filter((speaker) => speaker.send_protocol && speaker.email)
              .map((speaker) => `${speaker.name} (${speaker.email})`).join(", ")}.
          </p>}
          {meeting.name_review && (tab !== "transcript" || !meeting.segments?.length) && <p className="progress-hint">{meeting.name_review.message} Подробности — во вкладке «Транскрипт».</p>}
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
          {tab === "overview" && <Overview result={result} onSource={openSource} />}
          {tab === "actions" && <Actions result={result} onSource={openSource} />}
          {tab === "mom" && <>
            <button className="ghost word-copy" type="button" onClick={() => void copyWord()}>Копировать для Word</button>
            <Minutes meeting={meeting} result={result} contentRef={wordContent} />
          </>}
          {tab === "transcript" && <TranscriptView meeting={meeting} onUpdated={onUpdated} focusSegment={focusSegment} />}
        </>
      )}
    </section>
  );
}

function SpeakerReview({ meeting, mailEnabled, onUpdated }: {
  meeting: Meeting;
  mailEnabled: boolean;
  onUpdated: (meeting: Meeting) => void;
}) {
  const [edited, setEdited] = useState(false);
  const [protocolBusy, setProtocolBusy] = useState(false);
  const [speakerBusy, setSpeakerBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function buildProtocol(recipients: ProtocolRecipient[]) {
    setError(null);
    setProtocolBusy(true);
    try {
      onUpdated(await startProtocol(meeting.id, recipients));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось собрать протокол");
    } finally {
      setProtocolBusy(false);
    }
  }

  return (
    <section className="review-stage" aria-label="Проверка спикеров">
      <div className="review-intro">
        <h2>Расшифровка готова</h2>
        <p>При необходимости уточните имена и распределение реплик.</p>
        <ProtocolRecipients meeting={meeting} mailEnabled={mailEnabled}
          disabled={protocolBusy || speakerBusy || (mailEnabled && meeting.speakers == null)} busy={protocolBusy}
          submitLabel={edited ? "Собрать протокол" : "Пропустить и собрать протокол"} onSubmit={buildProtocol} />
        {error && <div className="banner" role="alert">{error}</div>}
      </div>
      <TranscriptView meeting={meeting} disabled={protocolBusy} onBusyChange={setSpeakerBusy}
        onUpdated={(next) => {
          setEdited(true);
          onUpdated(next);
        }} />
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

function protocolHint(message: string | null | undefined): string {
  const lead = (message || "").split("·")[0].trim();
  const named = lead.match(/^(.+?) пишет протокол/);
  const who = named?.[1] || "Модель";
  return `${who} пишет протокол. Процент обновится, когда она ответит — для длинной записи это несколько минут.`;
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

function ProcessCard({
  meeting,
  mailEnabled,
  onStop,
  onRestart,
}: {
  meeting: Meeting;
  mailEnabled: boolean;
  onStop: () => void;
  onRestart: () => void;
}) {
  const steps = meeting.source_type === "text" ? STEPS.filter((step) => step.id === "analyzing") : STEPS;
  const current = steps.findIndex((step) => step.id === meeting.status);
  const index = meeting.status === "queued" ? -1 : current;
  const queued = meeting.status === "queued";
  const pct = Math.max(0, Math.min(100, meeting.progress ?? fallbackProgress(meeting.status)));
  const stamp = queued ? `${meeting.id}|queued` : `${meeting.id}|${meeting.status}|${pct}|${meeting.status_message}`;
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
  const hint = queued && (meeting.source_type === "text" || meeting.transcript?.trim())
    ? "Транскрипт сохранён. Когда освободится слот, начнётся сборка протокола."
    : queued
    ? mailEnabled
      ? "Обработка начнётся, когда освободится слот. Готовый протокол придёт на почту."
      : "Обработка начнётся, когда освободится слот. Результат появится в MoM."
    : meeting.status === "analyzing"
      ? protocolHint(meeting.status_message)
      : meeting.status === "transcribing"
        ? transcribeHint(meeting.status_message)
        : "Обработка идёт.";
  return (
    <div className="process">
      <div className="progress-head" role={queued ? "status" : undefined} aria-live={queued ? "polite" : undefined}>
        <strong>{queued ? "В очереди" : `${pct}%`}</strong>
        <span>{queued ? queuePositionLabel(meeting.queue_ahead) : meeting.status_message || stepLabel(meeting.status)}</span>
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
      {queued && <p className="progress-live">{`Ожидание ${formatClock(elapsed)}`}</p>}
      <ol>
        {steps.map((step, i) => (
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
      {queued && <p className="progress-hint">Перед вами учитываются записи в обработке и более ранние заявки в очереди. Позиция обновляется автоматически.</p>}
      <div className="composer-row">
        <button className="ghost danger" type="button" onClick={onStop}>
          Остановить
        </button>
        <button className="ghost" type="button" onClick={onRestart}>
          Запустить заново
        </button>
      </div>
    </div>
  );
}

export function stepLabel(status: MeetingStatus): string {
  if (status === "queued") return "В очереди";
  if (status === "review") return "Проверьте спикеров";
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

function Overview({ result, onSource }: { result: MeetingResult; onSource: (turn: number) => void }) {
  return (
    <div className="overview">
      {result.participants.length > 0 && (
        <p className="people">Участники: {result.participants.join(", ")}</p>
      )}
      <SummarySections result={result} heading="h2" onSource={onSource} />
    </div>
  );
}

function SourceButtons({ turns, onSource }: { turns?: number[]; onSource?: (turn: number) => void }) {
  if (!onSource || !Array.isArray(turns) || !turns.length) return null;
  return <span className="source-links">{turns.filter((turn) => Number.isInteger(turn) && turn > 0).slice(0, 3).map((turn) => (
    <button key={turn} type="button" className="ghost" onClick={() => onSource(turn)}>Реплика {turn}</button>
  ))}</span>;
}

function SummarySections({ result, heading: Heading, onSource }: {
  result: MeetingResult; heading: "h2" | "h3"; onSource?: (turn: number) => void;
}) {
  const paragraphs = result.summary.split(/\r?\n/).map((para) => para.trim()).filter(Boolean);
  const sources = Array.isArray(result.decision_sources) ? result.decision_sources : [];
  return <>
    <section className="summary">
      <Heading>Саммари</Heading>
      {paragraphs.length ? paragraphs.map((para, index) => <p key={index}>{para}</p>) : <p>Саммари отсутствует.</p>}
    </section>
    {([
      ["Принятые решения", result.decisions, true],
      ["Контекст и открытые вопросы", result.key_points, false],
    ] as const).map(([label, entries, isDecision]) => entries.length > 0 && (
      <section className="summary-points" key={label}>
        <Heading>{label}</Heading>
        <ul>{entries.map((item, index) => <li key={index}>{item}
          {isDecision && <SourceButtons turns={sources.find((source) => source?.decision === item)?.turns} onSource={onSource} />}
        </li>)}</ul>
      </section>
    ))}
  </>;
}

function Actions({ result, onSource }: { result: MeetingResult; onSource: (turn: number) => void }) {
  if (result.action_items.length === 0) {
    return <p className="empty-note">В записи не нашлось явных поручений.</p>;
  }
  return (
    <ol className="action-list">
      {result.action_items.map((item, index) => (
        <li key={index}>
          <strong>{item.task}</strong>
          <dl>
            <div><dt>Исполнитель</dt><dd>{item.assignee || "Не назначен"}</dd></div>
            <div><dt>Срок</dt><dd>{item.due || "Не указан"}</dd></div>
            <div><dt>Приоритет</dt><dd className={`prio ${item.priority}`}>{priorityLabel(item.priority)}</dd></div>
          </dl>
          <SourceButtons turns={item.source_turns} onSource={onSource} />
        </li>
      ))}
    </ol>
  );
}

function ProtocolActions({ result }: { result: MeetingResult }) {
  if (!result.action_items.length) return <p>Поручений не зафиксировано.</p>;
  return (
    <table className="protocol-actions">
      <thead>
        <tr>
          <th scope="col">Поручение</th>
          <th scope="col">Исполнитель</th>
          <th scope="col">Срок</th>
          <th scope="col">Приоритет</th>
        </tr>
      </thead>
      <tbody>
        {result.action_items.map((item, index) => (
          <tr key={`${item.task}-${index}`}>
            <td>{item.task}</td>
            <td>{item.assignee || "Не назначен"}</td>
            <td>{item.due || "Не указан"}</td>
            <td>
              <span className={`prio ${item.priority}`}>{priorityLabel(item.priority)}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Minutes({ meeting, result, contentRef }: { meeting: Meeting; result: MeetingResult; contentRef: React.Ref<HTMLElement> }) {
  return (
    <article className="paper" ref={contentRef}>
      <header>
        <h2>{result.title || meeting.title}</h2>
        <ul>
          {result.date_hint && <li>Дата встречи: {result.date_hint}</li>}
          {meeting.duration_seconds && (
            <li>Длительность: {formatDuration(meeting.duration_seconds)}</li>
          )}
          {result.participants.length > 0 && (
            <li>Участники: {result.participants.join(", ")}</li>
          )}
        </ul>
      </header>
      <SummarySections result={result} heading="h3" />
      <section>
        <h3>Поручения</h3>
        <ProtocolActions result={result} />
      </section>
      {result.mom.agenda.length > 0 && <h3>Ход обсуждения</h3>}
      {(result.mom.agenda || []).map((block, index) => (
        <section key={index}>
          <h4>{block.topic}</h4>
          {block.discussion && <p>{block.discussion}</p>}
          {block.outcome && (
            <p className="outcome">
              <span>Итог.</span> {block.outcome}
            </p>
          )}
        </section>
      ))}
      {result.mom.next_meeting && (
        <section className="next"><h3>Следующая встреча</h3><p>{result.mom.next_meeting}</p></section>
      )}
    </article>
  );
}
