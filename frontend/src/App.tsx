import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createMeeting,
  createUser,
  deleteApiKey,
  deleteMeeting,
  downloadMarkdown,
  getAuthStatus,
  getHealth,
  getMeeting,
  getSettings,
  getUserSession,
  listMeetings,
  listUsers,
  loginAdmin,
  loginUser,
  logoutAdmin,
  logoutUser,
  openMeetingEmail,
  resetUserPassword,
  retryMeeting,
  saveApiKey,
  saveModels,
  setupAdmin,
  updateUserLimit,
  verifyApiKey,
} from "./api";
import type { AuthStatus, Health, Meeting, MeetingResult, MeetingStatus, OpenAIStatus, PlatformUser, UserSession } from "./types";

type Tab = "overview" | "actions" | "mom" | "transcript";

const STEPS: { id: MeetingStatus; label: string }[] = [
  { id: "extracting", label: "Аудио" },
  { id: "transcribing", label: "Расшифровка" },
  { id: "analyzing", label: "Протокол" },
];

const ACCEPT = ".webm,.mp4,.mp3,.wav,.m4a,.ogg,audio/webm,video/webm";

export default function App() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);
  const [screen, setScreen] = useState<"app" | "admin">("app");
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [user, setUser] = useState<UserSession | null>(null);
  const [adminHealth, setAdminHealth] = useState<Health | null>(null);
  const [adminTab, setAdminTab] = useState<"keys" | "users">("keys");

  const selected = useMemo(
    () => meetings.find((item) => item.id === selectedId) ?? null,
    [meetings, selectedId],
  );

  const refresh = useCallback(async () => {
    const rows = await listMeetings();
    setMeetings(rows);
    return rows;
  }, []);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch(() =>
        setHealth({
          ok: false,
          ffmpeg: false,
          openai: {
            configured: false,
            connected: false,
            hint: null,
            message: "Нет связи с сервером",
            checked_at: null,
          },
        }),
      );
    getAuthStatus().then(setAuth).catch(() => setAuth({ configured: false, authenticated: false }));
    getUserSession()
      .then((session) => {
        setUser(session);
        if (session.authenticated) refresh().catch((err: Error) => setError(err.message));
      })
      .catch(() => setUser({ authenticated: false }));
  }, [refresh]);

  useEffect(() => {
    if (!composing && !selectedId && meetings.length > 0) {
      setSelectedId(meetings[0].id);
    }
  }, [composing, meetings, selectedId]);

  useEffect(() => {
    if (!selected || selected.status === "done" || selected.status === "error") {
      return;
    }
    const timer = window.setInterval(async () => {
      try {
        const next = await getMeeting(selected.id);
        setMeetings((rows) => rows.map((row) => (row.id === next.id ? next : row)));
      } catch {
        /* keep last known state */
      }
    }, 700);
    return () => window.clearInterval(timer);
  }, [selected]);

  async function onUpload(file: File, title: string) {
    setError(null);
    setBusy(true);
    try {
      const created = await createMeeting(file, title);
      setMeetings((rows) => [created, ...rows.filter((row) => row.id !== created.id)].slice(0, user?.archive_limit || 5));
      setSelectedId(created.id);
      setTab("overview");
      setComposing(false);
      getUserSession().then(setUser).catch(() => undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить запись");
    } finally {
      setBusy(false);
    }
  }

  async function onRetry(id: string) {
    setError(null);
    try {
      const updated = await retryMeeting(id);
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)));
      setSelectedId(id);
      setScreen("app");
      setComposing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось повторить обработку");
    }
  }

  async function onDelete(id: string) {
    if (!window.confirm("Удалить встречу и протокол?")) return;
    await deleteMeeting(id);
    const rows = await refresh();
    setSelectedId((current) => {
      if (current !== id) return current;
      return rows[0]?.id ?? null;
    });
  }

  const openai = health?.openai;
  const keyReady = Boolean(openai?.connected);
  const userReady = Boolean(user?.authenticated);
  const showComposer = composing || meetings.length === 0;

  useEffect(() => {
    if (screen !== "admin" || !auth?.authenticated) return;
    getSettings()
      .then((next) => {
        setAdminHealth(next);
        setHealth(next);
      })
      .catch(() => setAuth((current) => ({ configured: current?.configured ?? true, authenticated: false })));
  }, [screen, auth?.authenticated]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="mark">MoM</span>
          <span className="brand-sub">Minutes of Meeting</span>
        </div>
        <nav className="app-tabs" aria-label="Разделы">
          <button
            className={screen === "app" ? "active" : ""}
            onClick={() => setScreen("app")}
          >
            Встречи
          </button>
          <button
            className={screen === "admin" ? "active" : ""}
            onClick={() => setScreen("admin")}
          >
            Администрирование
          </button>
        </nav>
        <p className={`top-status ${userReady ? "ok" : "missing"}`}>
          {userReady ? `${user?.email}` : "Нет входа в профиль"}
        </p>
      </header>
      <div className="workspace">
      <aside className="rail">
        {userReady ? (
          <button className="primary" onClick={() => { setScreen("app"); setComposing(true); }}>
            Новая запись
          </button>
        ) : (
          <p className="rail-label">Войдите, чтобы видеть архив</p>
        )}
        <div className="rail-label">Архив{userReady && user?.archive_limit ? ` · ${meetings.length}/${user.archive_limit}` : ""}</div>
        <ul className="meeting-list">
          {meetings.map((item) => (
            <li key={item.id}>
              <button
                className={item.id === selectedId && !showComposer && screen === "app" ? "active" : ""}
                onClick={() => {
                  setSelectedId(item.id);
                  setComposing(false);
                  setScreen("app");
                  setTab("overview");
                }}
              >
                <span className={`dot ${item.status}`} />
                <span className="meeting-copy">
                  <strong>{item.title}</strong>
                  <small>
                    {item.status !== "done" && item.status !== "error"
                      ? `${item.status_message || stepLabel(item.status)}${typeof item.progress === "number" ? ` · ${item.progress}%` : ""}`
                      : `${formatDate(item.created_at)}${item.duration_seconds ? ` · ${formatDuration(item.duration_seconds)}` : ""}`}
                  </small>
                </span>
              </button>
            </li>
          ))}
        </ul>
        {userReady && (
          <button
            type="button"
            className="ghost"
            style={{ margin: "8px 16px" }}
            onClick={() => {
              void logoutUser().then((session) => {
                setUser(session);
                setMeetings([]);
                setSelectedId(null);
                setComposing(false);
              });
            }}
          >
            Выйти из профиля
          </button>
        )}
        <button
          type="button"
          className={`conn ${connectionClass(openai)}`}
          onClick={() => setScreen("admin")}
        >
          <span className="conn-dot" />
          <span>
            <strong>{connectionLabel(openai)}</strong>
            <small>{openai?.hint || openai?.message || "Ключ не задан"}</small>
          </span>
        </button>
        {health && !health.ffmpeg && <p className="health-warn">Не найден ffmpeg.</p>}
      </aside>

      <main className="stage">
        {error && <div className="banner">{error}</div>}
        {screen === "admin" ? (
          <AdminSection
            auth={auth}
            health={adminHealth || health}
            tab={adminTab}
            onTab={setAdminTab}
            onAuth={setAuth}
            onHealth={(next) => {
              setAdminHealth(next);
              setHealth(next);
            }}
          />
        ) : !userReady ? (
          <UserLogin
            onLogin={async (session) => {
              setUser(session);
              setError(null);
              const rows = await refresh();
              setSelectedId(rows[0]?.id ?? null);
            }}
          />
        ) : showComposer ? (
          <Composer
            busy={busy}
            keyReady={keyReady}
            onOpenSettings={() => setScreen("admin")}
            onUpload={onUpload}
          />
        ) : selected ? (
          <MeetingPane
            meeting={selected}
            tab={tab}
            onTab={setTab}
            onRetry={() => void onRetry(selected.id)}
            onDelete={() => onDelete(selected.id)}
          />
        ) : (
          <Composer
            busy={busy}
            keyReady={keyReady}
            onOpenSettings={() => setScreen("admin")}
            onUpload={onUpload}
          />
        )}
      </main>
      </div>
    </div>
  );
}

function Composer({
  busy,
  keyReady,
  onOpenSettings,
  onUpload,
}: {
  busy: boolean;
  keyReady: boolean;
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

  return (
    <section className="composer">
      <p className="eyebrow">Обработка записей</p>
      <h1>Из webm — саммари, поручения и протокол</h1>
      <p className="lead">
        Загрузите запись конф-колла. Приложение извлечёт аудио, расшифрует речь и
        соберёт Minutes of Meeting: итоги, решения и список поручений.
      </p>
      {!keyReady && (
        <div className="key-box">
          <p>Ключ ещё не подключён. Без рабочего ключа запись обработать нельзя.</p>
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

function AdminSection({
  auth,
  health,
  tab,
  onTab,
  onAuth,
  onHealth,
}: {
  auth: AuthStatus | null;
  health: Health | null;
  tab: "keys" | "users";
  onTab: (tab: "keys" | "users") => void;
  onAuth: (auth: AuthStatus) => void;
  onHealth: (health: Health) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [setupToken, setSetupToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const configured = Boolean(auth?.configured);
  const needSetupToken = !configured && Boolean(auth?.setup_token_required);

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      const next = configured
        ? await loginAdmin(username.trim(), password)
        : await setupAdmin(username.trim(), password, setupToken.trim());
      onAuth(next);
      setPassword("");
      const settings = await getSettings();
      onHealth(settings);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось войти");
    } finally {
      setBusy(false);
    }
  }

  if (!auth?.authenticated) {
    return (
      <section className="composer">
        <p className="eyebrow">Доступ</p>
        <h1>Администрирование</h1>
        <p className="lead">
          {configured
            ? "Раздел закрыт логином и паролем администратора."
            : "Создайте логин и пароль администратора. Если установка была через install.sh, введите SETUP_TOKEN из вывода скрипта или файла .env."}
        </p>
        <form
          className="key-box"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <p>{configured ? "Вход" : "Первый вход — создание учётной записи"}</p>
          <input
            className="title-input"
            autoComplete="username"
            placeholder="Логин"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
          <input
            className="title-input"
            style={{ marginTop: 10 }}
            type="password"
            autoComplete={configured ? "current-password" : "new-password"}
            placeholder={configured ? "Пароль" : "Пароль, не короче 8 символов"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          {needSetupToken && (
            <input
              className="title-input"
              style={{ marginTop: 10 }}
              type="password"
              autoComplete="off"
              placeholder="SETUP_TOKEN из .env"
              value={setupToken}
              onChange={(event) => setSetupToken(event.target.value)}
            />
          )}
          <div className="composer-row">
            <button
              className="primary"
              type="submit"
              disabled={
                busy ||
                username.trim().length < 3 ||
                password.length < (configured ? 1 : 8) ||
                (needSetupToken && setupToken.trim().length < 8)
              }
            >
              {busy ? "Проверяю…" : configured ? "Войти" : "Создать и войти"}
            </button>
          </div>
        </form>
        {error && <div className="banner">{error}</div>}
      </section>
    );
  }

  return (
    <div>
      <nav className="tabs" style={{ marginBottom: 8 }}>
        <button className={tab === "keys" ? "active" : ""} onClick={() => onTab("keys")}>
          API-ключи
        </button>
        <button className={tab === "users" ? "active" : ""} onClick={() => onTab("users")}>
          Пользователи
        </button>
      </nav>
      {tab === "users" ? (
        <UsersPanel />
      ) : (
        <SettingsPanel health={health} username={auth?.username} onChange={onHealth} onLogout={onAuth} />
      )}
    </div>
  );
}

function UserLogin({ onLogin }: { onLogin: (session: UserSession) => Promise<void> }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <section className="composer">
      <p className="eyebrow">Профиль</p>
      <h1>Вход</h1>
      <p className="lead">
        Войдите по email и паролю, которые выдал администратор. В профиле хранятся
        последние протоколы — лимит архива задаёт администратор.
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
      {error && <div className="banner">{error}</div>}
    </section>
  );
}

function UsersPanel() {
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [email, setEmail] = useState("");
  const [limit, setLimit] = useState(5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [secret, setSecret] = useState<{ email: string; password: string } | null>(null);

  async function reload() {
    setUsers(await listUsers());
  }

  useEffect(() => {
    reload().catch((err: Error) => setError(err.message));
  }, []);

  async function onCreate() {
    setError(null);
    setBusy(true);
    try {
      const created = await createUser(email.trim(), limit);
      setSecret({ email: created.user.email, password: created.password });
      setEmail("");
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось создать пользователя");
    } finally {
      setBusy(false);
    }
  }

  async function onReset(id: string, userEmail: string) {
    if (!window.confirm(`Сбросить пароль для ${userEmail}?`)) return;
    setError(null);
    try {
      const next = await resetUserPassword(id);
      setSecret({ email: userEmail, password: next.password });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сбросить пароль");
    }
  }

  async function onLimit(id: string, value: number) {
    try {
      await updateUserLimit(id, value);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить лимит");
    }
  }

  return (
    <section className="composer">
      <p className="eyebrow">Профили</p>
      <h1>Пользователи</h1>
      <p className="lead">
        Администратор заводит профиль вручную: указывает email, система генерирует
        пароль. По умолчанию в архиве 5 последних протоколов, лимит можно задать
        отдельно для каждого.
      </p>
      <form
        className="key-box"
        onSubmit={(event) => {
          event.preventDefault();
          void onCreate();
        }}
      >
        <p>Новый пользователь</p>
        <input
          className="title-input"
          type="email"
          placeholder="email@company.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <input
          className="title-input"
          style={{ marginTop: 10 }}
          type="number"
          min={1}
          max={100}
          value={limit}
          onChange={(event) => setLimit(Number(event.target.value) || 5)}
        />
        <div className="composer-row">
          <button className="primary" type="submit" disabled={busy || !email.includes("@")}>
            {busy ? "Создаю…" : "Создать и сгенерировать пароль"}
          </button>
        </div>
      </form>
      {secret && (
        <div className="status-card ok">
          <p className="status-kicker">Пароль показывается один раз</p>
          <p>
            {secret.email}
            <br />
            <code>{secret.password}</code>
          </p>
          <button
            className="ghost"
            type="button"
            onClick={() => void navigator.clipboard.writeText(`${secret.email}\n${secret.password}`)}
          >
            Скопировать
          </button>
        </div>
      )}
      <div className="user-table">
        {users.length === 0 ? (
          <p className="lead">Пользователей пока нет.</p>
        ) : (
          users.map((item) => (
            <div className="user-row" key={item.id}>
              <div>
                <strong>{item.email}</strong>
                <small>
                  Архив {item.meeting_count ?? 0} / {item.archive_limit}
                </small>
              </div>
              <input
                className="title-input"
                type="number"
                min={1}
                max={100}
                defaultValue={item.archive_limit}
                onBlur={(event) => {
                  const value = Number(event.target.value) || 5;
                  if (value !== item.archive_limit) void onLimit(item.id, value);
                }}
              />
              <button className="ghost" type="button" onClick={() => void onReset(item.id, item.email)}>
                Сбросить пароль
              </button>
            </div>
          ))
        )}
      </div>
      {error && <div className="banner">{error}</div>}
    </section>
  );
}

function SettingsPanel({
  health,
  username,
  onChange,
  onLogout,
}: {
  health: Health | null;
  username?: string | null;
  onChange: (health: Health) => void;
  onLogout: (auth: AuthStatus) => void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [chatModel, setChatModel] = useState(health?.openai.chat_model || "");
  const [asrModel, setAsrModel] = useState(health?.openai.asr_model || "");
  const [busy, setBusy] = useState<"save" | "verify" | "delete" | "models" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const openai = health?.openai;

  useEffect(() => {
    if (health?.openai.chat_model) setChatModel(health.openai.chat_model);
    if (health?.openai.asr_model) setAsrModel(health.openai.asr_model);
  }, [health]);

  async function run(kind: "save" | "verify" | "delete" | "models", action: () => Promise<Health>) {
    setError(null);
    setBusy(kind);
    try {
      const next = await action();
      onChange(next);
      if (kind === "save") setApiKey("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось выполнить действие");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="composer">
      <p className="eyebrow">Администрирование</p>
      <h1>API-ключи</h1>
      <p className="lead">
        Раздел доступен только администратору{username ? ` (${username})` : ""}.
        Ключи QwenCloud и OpenAI хранятся локально в `.env`.
      </p>

      <div className={`status-card ${connectionClass(openai)}`}>
        <p className="status-kicker">{connectionLabel(openai)}</p>
        <p>{openai?.message || "Статус ещё не получен"}</p>
        <ul>
          {openai?.provider_label && <li>Провайдер: {openai.provider_label}</li>}
          {openai?.hint && <li>Ключ: {openai.hint}</li>}
          {openai?.chat_model && <li>Чат: {openai.chat_model}</li>}
          {openai?.asr_model && <li>Расшифровка: {openai.asr_model}</li>}
          {openai?.base_url && <li>Host: {openai.base_url}</li>}
          {openai?.checked_at && <li>Проверено: {formatDate(openai.checked_at)}</li>}
          <li>ffmpeg: {health?.ffmpeg ? "найден" : "не найден"}</li>
        </ul>
      </div>

      <form
        className="key-box"
        onSubmit={(event) => {
          event.preventDefault();
          void run("save", () => saveApiKey(apiKey.trim()));
        }}
      >
        <p>{openai?.configured ? "Заменить ключ" : "Добавить ключ Qwen или OpenAI"}</p>
        <div className="composer-row">
          <input
            className="title-input"
            type="password"
            autoComplete="off"
            placeholder="sk-ws-… или sk-…"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
          />
          <button className="primary" type="submit" disabled={busy !== null || apiKey.trim().length < 20}>
            {busy === "save" ? "Проверяю…" : "Сохранить и проверить"}
          </button>
        </div>
      </form>

      <form
        className="key-box"
        onSubmit={(event) => {
          event.preventDefault();
          void run("models", () => saveModels(chatModel.trim(), asrModel.trim()));
        }}
      >
        <p>Модели. Token Plan Individual не включает ASR — для расшифровки укажите local-whisper.</p>
        <input
          className="title-input"
          placeholder="Чат, например qwen3.7-plus"
          value={chatModel}
          onChange={(event) => setChatModel(event.target.value)}
        />
        <input
          className="title-input"
          style={{ marginTop: 10 }}
          placeholder="Расшифровка: local-whisper"
          value={asrModel}
          onChange={(event) => setAsrModel(event.target.value)}
        />
        <div className="composer-row">
          <button className="primary" type="submit" disabled={busy !== null || !chatModel.trim() || !asrModel.trim()}>
            {busy === "models" ? "Сохраняю…" : "Сохранить модели"}
          </button>
        </div>
      </form>

      <div className="pane-actions settings-actions">
        <button
          className="ghost"
          disabled={busy !== null || !openai?.configured}
          onClick={() => void run("verify", verifyApiKey)}
        >
          {busy === "verify" ? "Проверяю…" : "Проверить подключение"}
        </button>
        <button
          className="ghost danger"
          disabled={busy !== null || !openai?.configured}
          onClick={() => {
            if (!window.confirm("Удалить сохранённый ключ?")) return;
            void run("delete", deleteApiKey);
          }}
        >
          {busy === "delete" ? "Удаляю…" : "Удалить ключ"}
        </button>
        <button
          className="ghost"
          disabled={busy !== null}
          onClick={() => {
            void logoutAdmin()
              .then(onLogout)
              .catch((err: Error) => setError(err.message));
          }}
        >
          Выйти
        </button>
      </div>
      {error && <div className="banner">{error}</div>}
    </section>
  );
}

function connectionClass(status: OpenAIStatus | undefined): string {
  if (!status?.configured) return "missing";
  if (status.connected) return "ok";
  return "bad";
}

function connectionLabel(status: OpenAIStatus | undefined): string {
  const name = status?.provider_label || "API";
  if (!status?.configured) return "Ключ не задан";
  if (status.connected) return `${name} подключён`;
  return `${name}: ключ не принят`;
}

function MeetingPane({
  meeting,
  tab,
  onTab,
  onRetry,
  onDelete,
}: {
  meeting: Meeting;
  tab: Tab;
  onTab: (tab: Tab) => void;
  onRetry: () => void;
  onDelete: () => void;
}) {
  const processing = meeting.status !== "done" && meeting.status !== "error";
  const result = meeting.result;

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
                onClick={() => openMeetingEmail(meeting)}
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
          {meeting.status === "error" && (
            <button className="ghost" onClick={onRetry}>
              Повторить
            </button>
          )}
          <button className="ghost danger" onClick={onDelete}>
            Удалить
          </button>
        </div>
      </header>

      {processing && <ProcessCard meeting={meeting} />}
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

function ProcessCard({ meeting }: { meeting: Meeting }) {
  const current = STEPS.findIndex((step) => step.id === meeting.status);
  const index = meeting.status === "queued" ? -1 : current;
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
  const hint =
    meeting.status === "analyzing"
      ? "Qwen пишет протокол. Процент обновится, когда модель ответит — для длинной записи это несколько минут."
      : meeting.status === "transcribing"
        ? "Whisper идёт по записи, процент растёт вместе с таймкодом."
        : "Обработка идёт.";
  return (
    <div className="process">
      <div className="progress-head">
        <strong>{pct}%</strong>
        <span>{meeting.status_message || stepLabel(meeting.status)}</span>
      </div>
      <div
        className={`progress-bar${waiting ? " waiting" : ""}`}
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <i style={{ width: `${pct}%` }} />
      </div>
      <p className="progress-live">
        В работе {formatClock(elapsed)}
        {waiting ? " · процесс живой, ждём ответ" : ""}
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

function stepLabel(status: MeetingStatus): string {
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
        {result.summary.split("\n").filter(Boolean).map((para) => (
          <p key={para}>{para}</p>
        ))}
      </article>
      <div className="split">
        {result.key_points.length > 0 && (
          <section>
            <h2>Тезисы</h2>
            <ul>
              {result.key_points.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        )}
        {result.decisions.length > 0 && (
          <section>
            <h2>Решения</h2>
            <ul className="decisions">
              {result.decisions.map((item) => (
                <li key={item}>{item}</li>
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
      {(result.mom.agenda || []).map((block) => (
        <section key={block.topic}>
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

function count(items: unknown[]): string {
  return items.length ? ` · ${items.length}` : "";
}

function priorityLabel(value: string): string {
  if (value === "high") return "высокий";
  if (value === "low") return "низкий";
  return "средний";
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(seconds: number): string {
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours} ч ${minutes} мин`;
  return `${minutes} мин`;
}

function formatClock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}
