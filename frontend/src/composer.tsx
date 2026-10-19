
import { useState } from "react";
import { loginUser } from "./api";
import { uploadBlockedReason } from "./format";
import type { OpenAIStatus, UserSession } from "./types";

export const ACCEPT = ".webm,.mkv,.mp4,.mp3,.wav,.m4a,.ogg,audio/webm,video/webm,video/x-matroska";

export function Composer({
  busy,
  keyStatus,
  queue,
  mailReady,
  onOpenSettings,
  onUpload,
}: {
  busy: boolean;
  keyStatus: OpenAIStatus | null;
  queue?: { active: number; waiting: number; limit: number };
  mailReady: boolean;
  onOpenSettings: () => void;
  onUpload: (file: File | null, title: string, text?: string) => Promise<void>;
}) {
  const [mode, setMode] = useState<"audio" | "text">("audio");
  const [text, setText] = useState("");
  const [fileError, setFileError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [over, setOver] = useState(false);

  function takeFile(next: File | null) {
    if (!next) return;
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

  return (
    <section className="composer">
      <p className="eyebrow">Обработка записей</p>
      <h1>Саммари, поручения и протокол</h1>
      <p className="lead">
        Загрузите запись. Если слоты заняты, встреча встанет в очередь.
        Сколько обрабатывается сразу, задаёт администратор. {mailReady
          ? "Когда протокол будет готов, письмо придёт на email, с которым вы входите."
          : "Готовый протокол останется в MoM; почтовые уведомления включит администратор."}
      </p>
      {queued > 0 && (
        <p className="lead">
          Сейчас в работе {queue?.active ?? 0}
          {(queue?.waiting ?? 0) > 0 ? `, в очереди ${queue?.waiting}` : ""}.
        </p>
      )}
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
      {blocked && mode === "audio" && (
        <div className="key-box">
          <p>{blocked}</p>
          <button className="primary" type="button" onClick={onOpenSettings}>
            Открыть администрирование
          </button>
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
        <span className="drop-kicker">{mode === "text" ? "txt, md, srt, vtt · до 1 МБ" : "webm, mkv, mp4, mp3, wav"}</span>
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
          disabled={busy || (mode === "audio" ? !file || !keyReady : !file && !text.trim())}
          onClick={() => onUpload(file, title, mode === "text" ? text : undefined)}
        >
          {busy ? "Загружаю…" : mode === "text" ? "Импортировать транскрипт" : "Обработать"}
        </button>
      </div>
    </section>
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
