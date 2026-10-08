import { useEffect, useState } from "react";
import {
  createUser,
  deleteApiKey,
  getSettings,
  listUsers,
  loginAdmin,
  logoutAdmin,
  resetUserPassword,
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
import { encodeAsrModel, formatAsrLabel, parseAsrModel, WHISPER_SIZES, type AsrEngine } from "./asr";
import { connectionClass, connectionLabel, formatDate } from "./format";
import type { AdminTab, AuthStatus, Health, PlatformUser } from "./types";

export const ADMIN_TABS: { id: AdminTab; label: string }[] = [
  { id: "llm", label: "LLM" },
  { id: "asr", label: "Расшифровка" },
  { id: "queue", label: "Очередь" },
  { id: "smtp", label: "Почта" },
  { id: "theme", label: "Вид" },
  { id: "users", label: "Пользователи" },
];

export function AdminSection({
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
