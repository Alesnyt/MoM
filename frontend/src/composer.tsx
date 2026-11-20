
import { useState, type Dispatch, type SetStateAction } from "react";
import { CalendarPanel, CalendarContextCard } from "./calendar";
import type { CalendarEvent } from "./calendar-types";
import { loginUser } from "./api";
import { formatBytes, recordingSizeError, uploadBlockedReason } from "./format";
import type { OpenAIStatus, UserSession } from "./types";

export const ACCEPT = ".webm,.mkv,.mp4,.mp3,.wav,.m4a,.ogg,audio/webm,video/webm,video/x-matroska";

export type ComposerDraft = { mode: "audio" | "text"; text: string; title: string;
  file: File | null; calendar: CalendarEvent | null; priorTitle: string };
export const emptyComposerDraft = (): ComposerDraft => ({ mode: "audio", text: "", title: "", file: null, calendar: null, priorTitle: "" });

export function Composer({
  busy,
  serverUnavailable = false,
  keyStatus,
  queue,
  mailReady,
  maxUploadBytes,
  onOpenSettings,
  onUpload,
  nextExpired,
  userEmail,
  onOpenCalendarSettings,
  draft: savedDraft,
  onDraftChange,
}: {
  busy: boolean;
  serverUnavailable?: boolean;
  keyStatus: OpenAIStatus | null;
  queue?: { active: number; waiting: number; limit: number };
  mailReady: boolean;
  maxUploadBytes?: number;
  onOpenSettings?: () => void;
  onUpload: (file: File | null, title: string, text?: string, calendarEventId?: string) => Promise<void>;
  nextExpired?: string;
  userEmail?: string;
  onOpenCalendarSettings?: () => void;
  draft?: ComposerDraft;
  onDraftChange?: Dispatch<SetStateAction<ComposerDraft>>;
}) {
  const [localDraft, setLocalDraft] = useState(emptyComposerDraft);
  const { mode, text, title, file, calendar, priorTitle } = savedDraft ?? localDraft;
  const setDraft = onDraftChange ?? setLocalDraft;
  const setMode = (mode: ComposerDraft["mode"]) => setDraft((old) => ({ ...old, mode }));
  const setText = (text: string) => setDraft((old) => ({ ...old, text }));
  const setTitle = (title: string) => setDraft((old) => ({ ...old, title }));
  const setFile = (file: File | null) => setDraft((old) => ({ ...old, file }));
  const [fileError, setFileError] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [calendarPending, setCalendarPending] = useState(false);
  function selectCalendar(event: CalendarEvent | null) {
    if (event) {
      setDraft((old) => ({ ...old, calendar: event, title: event.title,
        priorTitle: calendar ? priorTitle : title }));
    } else setDraft((old) => ({ ...old, calendar: null,
      title: calendar && title === calendar.title ? priorTitle : title }));
  }

  function takeFile(next: File | null) {
    if (!next || busy) return;
    setFileError(null);
    if (mode === "text" && (!/\.(txt|md|srt|vtt)$/i.test(next.name) || next.size > 1024 * 1024)) {
      setFileError("Выберите .txt, .md, .srt или .vtt в UTF-8 размером до 1 МБ");
      setFile(null);
      return;
    }
    setFile(next);
    if (!title) setTitle(next.name.replace(/\.[^.]+$/, ""));
  }

  const queued = (queue?.active || 0) + (queue?.waiting || 0);
  const blocked = uploadBlockedReason(keyStatus);
  const keyReady = keyStatus?.connected === true;
  const limitReady = Number.isFinite(maxUploadBytes) && (maxUploadBytes ?? 0) > 0;
  const sizeError = mode === "audio" && file && limitReady
    ? recordingSizeError(file.size, maxUploadBytes!) : null;
  const cannotUpload = busy || serverUnavailable || calendarPending || (mode === "audio"
    ? !file || !keyReady || !limitReady || Boolean(sizeError)
    : !file && !text.trim());

  return (
    <div className="meeting-home">
    <section className="composer">
      <p className="eyebrow">Обработка записей</p>
      <h1>Саммари, поручения и протокол</h1>
      <p className="lead">
        Загрузите запись. Если слоты заняты, встреча встанет в очередь.
        Сколько обрабатывается сразу, задаёт администратор. {mailReady
          ? "Готовый протокол можно отредактировать и отправить выбранным получателям."
          : "Готовый протокол останется в MoM; почтовые уведомления включит администратор."}
      </p>
      {queued > 0 && (
        <p className="lead">
          Сейчас в работе {queue?.active ?? 0}
          {(queue?.waiting ?? 0) > 0 ? `, в очереди ${queue?.waiting}` : ""}.
        </p>
      )}
      {calendar && <CalendarContextCard event={calendar} onClear={busy ? undefined : () => selectCalendar(null)} />}
      <div className="composer-row" role="group" aria-label="Источник встречи">
        {(["audio", "text"] as const).map((value) => (
          <button key={value} type="button" className={mode === value ? "primary" : "ghost"}
            aria-pressed={mode === value} disabled={busy}
            onClick={() => { setMode(value); setFile(null); setFileError(null); }}>
            {value === "audio" ? "Аудио / видео" : "Готовый транскрипт"}
          </button>
        ))}
      </div>
      {mode === "text" && <>
        <p className="lead">Вставьте текст или выберите файл в UTF-8 до 1 МБ. Имена сохранятся.
          После импорта проверьте спикеров и нажмите «Собрать протокол».</p>
        <label htmlFor="transcript-input">Текст транскрипта</label>
        <textarea id="transcript-input" className="title-input" rows={9}
          style={{ width: "100%", boxSizing: "border-box", resize: "vertical" }}
          disabled={busy || Boolean(file)} value={text} maxLength={1024 * 1024}
          placeholder={"[00:05] Анна: Обсудим план.\nИван: Подготовлю к пятнице."}
          onChange={(event) => setText(event.target.value)} />
        {file && <button className="ghost" disabled={busy} onClick={() => setFile(null)}>Убрать файл и вставить текст</button>}
      </>}
      {fileError && <div className="banner" role="alert">{fileError}</div>}
      {sizeError && <div className="banner" role="alert">{sizeError}</div>}
      {blocked && mode === "audio" && (
        <div className="key-box">
          <p role="status">{onOpenSettings ? blocked : "Обработка записей сейчас недоступна. Попробуйте позже."}</p>
          {onOpenSettings && (
            <button className="primary" type="button" onClick={onOpenSettings}>
              Открыть администрирование
            </button>
          )}
        </div>
      )}

      <label
        className={`drop ${over ? "over" : ""} ${file ? "has-file" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          takeFile(event.dataTransfer.files[0] ?? null);
        }}
      >
        <input
          key={mode + (file?.name || "")}
          type="file"
          disabled={busy}
          accept={mode === "text" ? ".txt,.md,.srt,.vtt" : ACCEPT}
          onChange={(event) => takeFile(event.target.files?.[0] ?? null)}
        />
        <span className="drop-kicker">{mode === "text" ? "txt, md, srt, vtt · до 1 МБ"
          : `webm, mkv, mp4, mp3, wav · ${limitReady ? `до ${formatBytes(maxUploadBytes!)}` : "получаю лимит загрузки…"}`}</span>
        <strong>{file ? file.name : mode === "text" ? "Выбрать файл транскрипта" : "Перетащите запись сюда"}</strong>
        <small>
          {file
            ? `${(file.size / (1024 * 1024)).toFixed(1)} МБ`
            : "или нажмите, чтобы выбрать файл"}
        </small>
      </label>

      <div className="composer-row">
        <input
          className="title-input"
          placeholder="Название встречи (необязательно)"
          value={title}
          maxLength={200}
          aria-label="Название встречи"
          onChange={(event) => setTitle(event.target.value)}
        />
        <button
          className="primary"
          disabled={cannotUpload}
          onClick={() => { if (!cannotUpload) void onUpload(file, title, mode === "text" ? text : undefined, calendar?.id); }}
        >
          {busy ? "Загружаю…" : mode === "text" ? "Импортировать транскрипт" : "Обработать"}
        </button>
      </div>
      {nextExpired && <p className="progress-hint">Архив заполнен. Когда новая встреча завершится, из архива может быть удалена «{nextExpired}».</p>}
    </section>
    {userEmail && <CalendarPanel userEmail={userEmail} selectedId={calendar?.id} onSelect={selectCalendar}
      onPending={setCalendarPending} disabled={busy} onOpenSettings={onOpenCalendarSettings} />}
    </div>
  );
}


export function UserLogin({ onLogin }: { onLogin: (session: UserSession) => Promise<void> }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <section className="composer">
      <p className="eyebrow">Профиль</p>
      <h1>Вход</h1>
      <p className="lead">
        Войдите по email. Пароль выдаёт администратор или, если профиль привязан к каталогу, это пароль LDAP.
      </p>
      <form
        className="key-box"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setBusy(true);
          void loginUser(email.trim(), password)
            .then(onLogin)
            .catch((err: Error) => setError(err.message))
            .finally(() => setBusy(false));
        }}
      >
        <p>Email и пароль</p>
        <input
          className="title-input"
          type="email"
          autoComplete="username"
          placeholder="email@company.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <input
          className="title-input"
          style={{ marginTop: 10 }}
          type="password"
          autoComplete="current-password"
          placeholder="Пароль"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <div className="composer-row">
          <button className="primary" type="submit" disabled={busy || !email.includes("@") || password.length < 1}>
            {busy ? "Вхожу…" : "Войти"}
          </button>
        </div>
      </form>
      {error && <div className="banner" role="alert">{error}</div>}
    </section>
  );
}
