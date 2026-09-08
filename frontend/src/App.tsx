import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createMeeting,
  createUser,
  deleteApiKey,
  deleteMeeting,
  downloadMarkdown,
  buildEmailBody,
  getAuthStatus,
  getHealth,
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
  saveQueue,
  saveSmtp,
  saveTheme,
  setupAdmin,
  testSmtp,
  updateUserLimit,
  verifyApiKey,
} from "./api";
import type { AuthStatus, Health, Meeting, MeetingResult, MeetingStatus, OpenAIStatus, PlatformUser, UserSession } from "./types";

type Tab = "overview" | "actions" | "mom" | "transcript";
type AdminTab = "llm" | "asr" | "queue" | "smtp" | "theme" | "users";

const ADMIN_TABS: { id: AdminTab; label: string }[] = [
  { id: "llm", label: "LLM" },
  { id: "asr", label: "Расшифровка" },
  { id: "queue", label: "Очередь" },
  { id: "smtp", label: "Почта" },
  { id: "theme", label: "Вид" },
  { id: "users", label: "Пользователи" },
];

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
  const [adminTab, setAdminTab] = useState<AdminTab>("llm");

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
          theme: "classic",
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
    document.documentElement.dataset.theme = health?.theme === "t2" ? "t2" : "classic";
  }, [health?.theme]);

  useEffect(() => {
    if (!composing && !selectedId && meetings.length > 0) {
      setSelectedId(meetings[0].id);
    }
  }, [composing, meetings, selectedId]);

  const openai = health?.openai;
  const keyReady = Boolean(openai?.connected);
  const userReady = Boolean(user?.authenticated);
  const showComposer = composing || meetings.length === 0;
  const processingKey = meetings
    .filter((item) => item.status !== "done" && item.status !== "error")
    .map((item) => item.id)
    .join(",");

  useEffect(() => {
    if (!userReady || !processingKey) return;
    const timer = window.setInterval(async () => {
      try {
        const rows = await listMeetings();
        setMeetings(rows);
      } catch {
        /* keep last known state */
      }
    }, 700);
    return () => window.clearInterval(timer);
  }, [processingKey, userReady]);

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
          <button className="primary" onClick={() => {
            setScreen("app");
            setComposing(true);
            getHealth().then(setHealth).catch(() => undefined);
          }}>
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
                      ? item.status === "queued"
                        ? item.status_message || stepLabel(item.status)
                        : `${item.status_message || stepLabel(item.status)}${typeof item.progress === "number" ? ` · ${item.progress}%` : ""}`
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
            queue={health?.queue}
            mailReady={Boolean(health?.smtp?.configured)}
            onOpenSettings={() => setScreen("admin")}
            onUpload={onUpload}
          />
        ) : selected ? (
          <MeetingPane
            meeting={selected}
            tab={tab}
            mailEnabled={Boolean(health?.smtp?.configured)}
            onTab={setTab}
            onRetry={() => void onRetry(selected.id)}
            onDelete={() => onDelete(selected.id)}
          />
        ) : (
          <Composer
            busy={busy}
            keyReady={keyReady}
            queue={health?.queue}
            mailReady={Boolean(health?.smtp?.configured)}
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
  queue,
  mailReady,
  onOpenSettings,
  onUpload,
}: {
  busy: boolean;
  keyReady: boolean;
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
  tab: AdminTab;
  onTab: (tab: AdminTab) => void;
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
    <section className="composer admin-page">
      <header className="admin-head">
        <div>
          <p className="eyebrow">Администрирование{auth?.username ? ` · ${auth.username}` : ""}</p>
          <h1>{adminHeading(tab).title}</h1>
          <p className="lead">{adminHeading(tab).lead}</p>
        </div>
        <button
          className="ghost"
          type="button"
          onClick={() => {
            void logoutAdmin()
              .then(onAuth)
              .catch((err: Error) => setError(err.message));
          }}
        >
          Выйти
        </button>
      </header>
      <nav className="admin-tabs" aria-label="Настройки">
        {ADMIN_TABS.map((item) => (
          <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => onTab(item.id)}>
            {item.label}
          </button>
        ))}
      </nav>
      <div className="status-pills" aria-label="Сводка">
        <span className={`status-pill ${connectionClass(health?.openai)}`}>{connectionLabel(health?.openai)}</span>
        <span className={`status-pill ${health?.ffmpeg ? "ok" : "bad"}`}>
          ffmpeg {health?.ffmpeg ? "найден" : "нет"}
        </span>
        <span className="status-pill">
          Очередь {health?.queue?.active ?? 0}/{health?.queue?.limit ?? 1}
          {(health?.queue?.waiting ?? 0) > 0 ? ` · ждут ${health?.queue?.waiting}` : ""}
        </span>
        <span className={`status-pill ${health?.smtp?.configured ? "ok" : ""}`}>
          {health?.smtp?.configured ? `Почта ${health.smtp.host || "вкл."}` : "Почта выкл."}
        </span>
      </div>
      {tab === "users" ? (
        <UsersPanel />
      ) : (
        <SettingsPanel topic={tab} health={health} onChange={onHealth} />
      )}
      {error && <div className="banner">{error}</div>}
    </section>
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
    <>
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
    </>
  );
}

const WHISPER_SIZES = [
  { id: "tiny", label: "tiny — быстрее, хуже качество" },
  { id: "base", label: "base" },
  { id: "small", label: "small — по умолчанию" },
  { id: "medium", label: "medium — точнее, больше RAM" },
  { id: "large-v2", label: "large-v2" },
  { id: "large-v3", label: "large-v3 — максимум качества" },
] as const;

type AsrEngine = "whisper" | "gigaam" | "cloud";

type AsrChoice = {
  engine: AsrEngine;
  whisperSize: string;
  gigaamVariant: "ctc" | "large";
  cloudModel: string;
};

function parseAsrModel(model: string): AsrChoice {
  const name = (model || "").trim().toLowerCase();
  if (name.startsWith("gigaam") || name === "sber" || name.startsWith("sber-")) {
    return {
      engine: "gigaam",
      whisperSize: "small",
      gigaamVariant: name.includes("large") ? "large" : "ctc",
      cloudModel: "",
    };
  }
  if (!name || name.startsWith("local") || name === "faster-whisper") {
    let size = "small";
    const tagged = name.match(/local-whisper-(.+)$/);
    const short = name.match(/^local-(tiny|base|small|medium|large-v2|large-v3)$/);
    if (tagged) size = tagged[1];
    else if (short) size = short[1];
    return { engine: "whisper", whisperSize: size, gigaamVariant: "ctc", cloudModel: "" };
  }
  return { engine: "cloud", whisperSize: "small", gigaamVariant: "ctc", cloudModel: model };
}

function encodeAsrModel(choice: AsrChoice): string {
  if (choice.engine === "gigaam") {
    return choice.gigaamVariant === "large" ? "gigaam-multilingual-large" : "gigaam-multilingual";
  }
  if (choice.engine === "whisper") {
    return choice.whisperSize === "small" ? "local-whisper" : `local-whisper-${choice.whisperSize}`;
  }
  return choice.cloudModel.trim();
}

function formatAsrLabel(model: string | null | undefined): string {
  if (!model) return "";
  const asr = parseAsrModel(model);
  if (asr.engine === "gigaam") {
    return asr.gigaamVariant === "large"
      ? "Сбер GigaAM Multilingual 600M"
      : "Сбер GigaAM Multilingual 220M";
  }
  if (asr.engine === "whisper") return `Whisper ${asr.whisperSize}`;
  return model;
}

function adminHeading(tab: AdminTab): { title: string; lead: string } {
  if (tab === "llm") {
    return {
      title: "LLM и ключ",
      lead: "Ключ Qwen или OpenAI и модель чата. Она пишет саммари и протокол.",
    };
  }
  if (tab === "asr") {
    return {
      title: "Расшифровка",
      lead: "Движок распознавания речи: локальный Whisper, Сбер GigaAM или облако.",
    };
  }
  if (tab === "queue") {
    return {
      title: "Очередь",
      lead: "Сколько встреч запускать сразу. Если слотов не хватает, новые ждут в очереди.",
    };
  }
  if (tab === "smtp") {
    return {
      title: "Почта",
      lead: "Когда протокол готов, письмо уходит на email пользователя. Без SMTP обработка не ломается.",
    };
  }
  if (tab === "theme") {
    return {
      title: "Вид",
      lead: "Тема интерфейса по умолчанию для всех пользователей.",
    };
  }
  return {
    title: "Пользователи",
    lead: "Профиль заводится вручную: указываете email, система генерирует пароль. Лимит архива задаётся отдельно.",
  };
}

function SettingsPanel({
  topic,
  health,
  onChange,
}: {
  topic: Exclude<AdminTab, "users">;
  health: Health | null;
  onChange: (health: Health) => void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [chatModel, setChatModel] = useState(health?.openai.chat_model || "");
  const [asrModel, setAsrModel] = useState(health?.openai.asr_model || "");
  const [smtpHost, setSmtpHost] = useState(health?.smtp?.host || "");
  const [smtpPort, setSmtpPort] = useState(String(health?.smtp?.port || 587));
  const [smtpUser, setSmtpUser] = useState(health?.smtp?.user || "");
  const [smtpPassword, setSmtpPassword] = useState("");
  const [smtpFrom, setSmtpFrom] = useState(health?.smtp?.from_addr || "");
  const [smtpStarttls, setSmtpStarttls] = useState(health?.smtp?.starttls !== false);
  const [maxJobs, setMaxJobs] = useState(String(health?.queue?.limit || 1));
  const [publicUrl, setPublicUrl] = useState(health?.smtp?.public_url || "");
  const [smtpNote, setSmtpNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | "verify" | "delete" | "models" | "theme" | "smtp" | "smtp-test" | "queue" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const openai = health?.openai;
  const asr = parseAsrModel(asrModel);

  useEffect(() => {
    if (health?.openai.chat_model) setChatModel(health.openai.chat_model);
    if (health?.openai.asr_model) setAsrModel(health.openai.asr_model);
    if (health?.smtp) {
      setSmtpHost(health.smtp.host || "");
      setSmtpPort(String(health.smtp.port || 587));
      setSmtpUser(health.smtp.user || "");
      setSmtpFrom(health.smtp.from_addr || "");
      setSmtpStarttls(health.smtp.starttls !== false);
      setPublicUrl(health.smtp.public_url || "");
    }
    if (health?.queue?.limit) setMaxJobs(String(health.queue.limit));
  }, [health]);

  async function run(
    kind: "save" | "verify" | "delete" | "models" | "theme" | "smtp" | "queue",
    action: () => Promise<Health>,
  ) {
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

  function setEngine(engine: AsrEngine) {
    setAsrModel(
      encodeAsrModel({
        ...asr,
        engine,
        cloudModel: asr.cloudModel || "whisper-1",
      }),
    );
  }

  return (
    <>
      {topic === "llm" && (
        <>
          <div className={`status-card ${connectionClass(openai)}`}>
            <p className="status-kicker">{connectionLabel(openai)}</p>
            <p>{openai?.message || "Статус ещё не получен"}</p>
            <ul>
              {openai?.provider_label && <li>Провайдер: {openai.provider_label}</li>}
              {openai?.hint && <li>Ключ: {openai.hint}</li>}
              {openai?.chat_model && <li>Чат: {openai.chat_model}</li>}
              {openai?.base_url && <li>Host: {openai.base_url}</li>}
              {openai?.checked_at && <li>Проверено: {formatDate(openai.checked_at)}</li>}
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
              void run("models", () => saveModels(chatModel.trim(), asrModel.trim() || health?.openai.asr_model || "local-whisper"));
            }}
          >
            <p>Модель, которая собирает протокол.</p>
            <label className="field-label">
              Чат
              <input
                className="title-input"
                placeholder="Например qwen3.7-plus"
                value={chatModel}
                onChange={(event) => setChatModel(event.target.value)}
              />
            </label>
            <div className="composer-row">
              <button className="primary" type="submit" disabled={busy !== null || !chatModel.trim()}>
                {busy === "models" ? "Сохраняю…" : "Сохранить модель"}
              </button>
            </div>
          </form>

          <div className="pane-actions settings-actions">
            <button
              className="ghost"
              type="button"
              disabled={busy !== null || !openai?.configured}
              onClick={() => void run("verify", verifyApiKey)}
            >
              {busy === "verify" ? "Проверяю…" : "Проверить подключение"}
            </button>
            <button
              className="ghost danger"
              type="button"
              disabled={busy !== null || !openai?.configured}
              onClick={() => {
                if (!window.confirm("Удалить сохранённый ключ?")) return;
                void run("delete", deleteApiKey);
              }}
            >
              {busy === "delete" ? "Удаляю…" : "Удалить ключ"}
            </button>
          </div>
        </>
      )}

      {topic === "asr" && (
        <form
          className="key-box"
          onSubmit={(event) => {
            event.preventDefault();
            void run("models", () =>
              saveModels(chatModel.trim() || health?.openai.chat_model || "qwen3.7-plus", asrModel.trim()),
            );
          }}
        >
          <p>
            Сейчас: {formatAsrLabel(openai?.asr_model) || "не задано"}. Token Plan Individual не включает облачный ASR —
            берите Whisper или GigaAM.
          </p>
          <div className="choice-grid">
            <button
              type="button"
              className={`choice-card ${asr.engine === "whisper" ? "active" : ""}`}
              onClick={() => setEngine("whisper")}
            >
              <strong>Whisper</strong>
              <small>Локально, CPU, меньше RAM</small>
            </button>
            <button
              type="button"
              className={`choice-card ${asr.engine === "gigaam" ? "active" : ""}`}
              onClick={() => setEngine("gigaam")}
            >
              <strong>GigaAM</strong>
              <small>Сбер, локально, 2026</small>
            </button>
            <button
              type="button"
              className={`choice-card ${asr.engine === "cloud" ? "active" : ""}`}
              onClick={() => setEngine("cloud")}
            >
              <strong>Облако</strong>
              <small>Qwen ASR или whisper-1</small>
            </button>
          </div>
          <div className="field-stack" style={{ marginTop: 14 }}>
            {asr.engine === "whisper" && (
              <label className="field-label">
                Модель Whisper
                <select
                  className="title-input"
                  value={asr.whisperSize}
                  onChange={(event) => {
                    setAsrModel(encodeAsrModel({ ...asr, whisperSize: event.target.value }));
                  }}
                >
                  {WHISPER_SIZES.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {asr.engine === "gigaam" && (
              <>
                <label className="field-label">
                  Модель GigaAM
                  <select
                    className="title-input"
                    value={asr.gigaamVariant}
                    onChange={(event) => {
                      setAsrModel(
                        encodeAsrModel({
                          ...asr,
                          gigaamVariant: event.target.value === "large" ? "large" : "ctc",
                        }),
                      );
                    }}
                  >
                    <option value="ctc">220M CTC — быстрее, меньше RAM</option>
                    <option value="large">600M CTC — точнее, тяжелее</option>
                  </select>
                </label>
                <p>
                  Пакеты ставятся с проектом (`install.sh` / `update.sh`). Модель скачается с Hugging Face при первой
                  расшифровке.
                </p>
              </>
            )}
            {asr.engine === "cloud" && (
              <label className="field-label">
                Облачная модель
                <input
                  className="title-input"
                  placeholder="qwen3-asr-flash или whisper-1"
                  value={asr.cloudModel}
                  onChange={(event) => {
                    setAsrModel(encodeAsrModel({ ...asr, cloudModel: event.target.value }));
                  }}
                />
              </label>
            )}
          </div>
          <div className="composer-row">
            <button className="primary" type="submit" disabled={busy !== null || !asrModel.trim()}>
              {busy === "models" ? "Сохраняю…" : "Сохранить расшифровку"}
            </button>
          </div>
        </form>
      )}

      {topic === "queue" && (
        <form
          className="key-box"
          onSubmit={(event) => {
            event.preventDefault();
            void run("queue", () => saveQueue(Math.min(8, Math.max(1, Number(maxJobs) || 1))));
          }}
        >
          <p>
            1 — спокойно для 4–8 ГБ. 2–4, если RAM и CPU позволяют: ffmpeg и Qwen идут параллельно. Локальный
            Whisper/GigaAM в памяти один, распознавание не грузит вторую копию модели.
          </p>
          {health?.queue && (
            <p>
              Сейчас в работе {health.queue.active}, в очереди {health.queue.waiting}, слотов {health.queue.limit}.
            </p>
          )}
          <label className="field-label">
            Одновременно в работе
            <select className="title-input" value={maxJobs} onChange={(event) => setMaxJobs(event.target.value)}>
              {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
                <option key={n} value={String(n)}>
                  {n === 1
                    ? "1 — по умолчанию, мало RAM"
                    : n === 4
                      ? "4 — 16+ ГБ RAM"
                      : n === 8
                        ? "8 — много ядер и облачный ASR"
                        : String(n)}
                </option>
              ))}
            </select>
          </label>
          <div className="composer-row">
            <button className="primary" type="submit" disabled={busy !== null}>
              {busy === "queue" ? "Сохраняю…" : "Сохранить очередь"}
            </button>
          </div>
        </form>
      )}

      {topic === "smtp" && (
        <form
          className="key-box"
          onSubmit={(event) => {
            event.preventDefault();
            setSmtpNote(null);
            void run("smtp", () =>
              saveSmtp({
                host: smtpHost.trim(),
                port: Number(smtpPort) || 587,
                user: smtpUser.trim(),
                password: smtpPassword,
                from_addr: smtpFrom.trim(),
                starttls: smtpStarttls,
                public_url: publicUrl.trim(),
              }).then((next) => {
                setSmtpPassword("");
                return next;
              }),
            );
          }}
        >
          <p>
            {health?.smtp?.configured
              ? `Сервер: ${health.smtp.host || "задан"} · порт ${health.smtp.port ?? 587}`
              : "SMTP ещё не задан — письма не уходят, протоколы остаются в MoM."}
          </p>
          <div className="field-stack">
            <label className="field-label">
              SMTP-сервер
              <input
                className="title-input"
                placeholder="smtp.example.com"
                value={smtpHost}
                onChange={(event) => setSmtpHost(event.target.value)}
                autoComplete="off"
              />
            </label>
            <label className="field-label">
              Порт
              <input
                className="title-input"
                type="number"
                min={1}
                max={65535}
                value={smtpPort}
                onChange={(event) => setSmtpPort(event.target.value)}
              />
            </label>
            <label className="field-label">
              Логин SMTP
              <input
                className="title-input"
                value={smtpUser}
                onChange={(event) => setSmtpUser(event.target.value)}
                autoComplete="off"
              />
            </label>
            <label className="field-label">
              Пароль SMTP
              <input
                className="title-input"
                type="password"
                autoComplete="new-password"
                placeholder={health?.smtp?.has_password ? "Задан, оставьте пустым чтобы не менять" : "Пароль"}
                value={smtpPassword}
                onChange={(event) => setSmtpPassword(event.target.value)}
              />
            </label>
            <label className="field-label">
              От кого
              <input
                className="title-input"
                placeholder="mom@example.com"
                value={smtpFrom}
                onChange={(event) => setSmtpFrom(event.target.value)}
                autoComplete="off"
              />
            </label>
            <label className="field-check">
              <input
                type="checkbox"
                checked={smtpStarttls}
                onChange={(event) => setSmtpStarttls(event.target.checked)}
              />
              STARTTLS (для порта 587)
            </label>
            <label className="field-label">
              Ссылка на MoM в письме (необязательно)
              <input
                className="title-input"
                placeholder="https://mom.example.com"
                value={publicUrl}
                onChange={(event) => setPublicUrl(event.target.value)}
                autoComplete="off"
              />
            </label>
          </div>
          <div className="composer-row">
            <button className="primary" type="submit" disabled={busy !== null}>
              {busy === "smtp" ? "Сохраняю…" : "Сохранить почту"}
            </button>
            <button
              className="ghost"
              type="button"
              disabled={busy !== null || !health?.smtp?.configured}
              onClick={() => {
                setSmtpNote(null);
                setBusy("smtp-test");
                void testSmtp(smtpFrom.trim() || smtpUser.trim())
                  .then((result) => setSmtpNote(`Тестовое письмо отправлено на ${result.to}`))
                  .catch((err: Error) => setError(err.message))
                  .finally(() => setBusy(null));
              }}
            >
              {busy === "smtp-test" ? "Отправляю…" : "Проверить SMTP"}
            </button>
          </div>
          {smtpNote && <p>{smtpNote}</p>}
        </form>
      )}

      {topic === "theme" && (
        <div className="key-box">
          <p>Тема применяется сразу для всех, кто открывает MoM.</p>
          <div className="theme-grid">
            <button
              type="button"
              className={`theme-card ${(health?.theme || "classic") === "classic" ? "active" : ""}`}
              disabled={busy !== null}
              onClick={() => void run("theme", () => saveTheme("classic"))}
            >
              <span className="theme-swatch" aria-hidden="true">
                <i style={{ background: "#101218" }} />
                <i style={{ background: "#c9843e" }} />
                <i style={{ background: "#f1e6cf" }} />
              </span>
              <strong>Классическая</strong>
              <small>Тёмная тема — по умолчанию</small>
            </button>
            <button
              type="button"
              className={`theme-card ${health?.theme === "t2" ? "active" : ""}`}
              disabled={busy !== null}
              onClick={() => void run("theme", () => saveTheme("t2"))}
            >
              <span className="theme-swatch" aria-hidden="true">
                <i style={{ background: "#000000" }} />
                <i style={{ background: "#ff3495" }} />
                <i style={{ background: "#ffffff" }} />
              </span>
              <strong>T2</strong>
              <small>Чёрный, розовый #FF3495 и белый</small>
            </button>
          </div>
          {busy === "theme" && <p>Сохраняю тему…</p>}
        </div>
      )}

      {error && <div className="banner">{error}</div>}
    </>
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
        : buildEmailBody(meeting);
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
