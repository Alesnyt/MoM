
import { useState } from "react";
import { loginUser } from "./api";
import { uploadBlockedReason } from "./format";
import type { OpenAIStatus, UserSession } from "./types";

export const ACCEPT = ".webm,.mp4,.mp3,.wav,.m4a,.ogg,audio/webm,video/webm";

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
  onUpload: (file: File, title: string) => Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [over, setOver] = useState(false);

  function takeFile(next: File | null) {
    if (!next) return;
    setFile(next);
    if (!title) setTitle(next.name.replace(/\.[^.]+$/, ""));
  }

  const queued = (queue?.active || 0) + (queue?.waiting || 0);
  const blocked = uploadBlockedReason(keyStatus);
  const keyReady = keyStatus?.connected === true;

  return (
    <section className="composer">
      <p className="eyebrow">Обработка записей</p>
      <h1>Из webm — саммари, поручения и протокол</h1>
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
      {blocked && (
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
          type="file"
          accept={ACCEPT}
          onChange={(event) => takeFile(event.target.files?.[0] ?? null)}
        />
        <span className="drop-kicker">webm, mp4, mp3, wav</span>
        <strong>{file ? file.name : "Перетащите запись сюда"}</strong>
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
          onChange={(event) => setTitle(event.target.value)}
        />
        <button
          className="primary"
          disabled={!file || busy || !keyReady}
          onClick={() => file && onUpload(file, title)}
        >
          {busy ? "Загружаю…" : "Обработать"}
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
