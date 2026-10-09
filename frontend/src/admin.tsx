import { useEffect, useState } from "react";
import {
  createUser,
  deleteApiKey,
  getSettings,
  listAudit,
  listUsers,
  loginAdmin,
  logoutAdmin,
  deleteUser,
  resetUserPassword,
  saveApiKey,
  saveModels,
  saveQueue,
  saveSmtp,
  saveLdap,
  saveTheme,
  setUserAuthMode,
  setupAdmin,
  testLdap,
  testSmtp,
  updateUserLimit,
  verifyApiKey,
} from "./api";
import { encodeAsrModel, formatAsrLabel, parseAsrModel, WHISPER_SIZES, type AsrEngine } from "./asr";
import { connectionClass, connectionLabel, formatBytes, formatDate } from "./format";
import type { AdminTab, AuditEvent, AuthStatus, Health, PlatformUser } from "./types";

export const ADMIN_TABS: { id: AdminTab; label: string }[] = [
  { id: "llm", label: "LLM" },
  { id: "asr", label: "Расшифровка" },
  { id: "queue", label: "Очередь" },
  { id: "smtp", label: "Почта" },
  { id: "ldap", label: "Каталог" },
  { id: "theme", label: "Вид" },
  { id: "users", label: "Пользователи" },
  { id: "audit", label: "Журнал" },
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

  if (auth === null) {
    return (
      <section className="composer" aria-busy="true">
        <p className="eyebrow">Доступ</p>
        <h1>Администрирование</h1>
        <p className="lead">Проверяю доступ…</p>
      </section>
    );
  }

  if (!auth.authenticated) {
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
        {error && <div className="banner" role="alert">{error}</div>}
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
              .then((next) => {
                setUsername("");
                setPassword("");
                onAuth(next);
              })
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
        <span className={`status-pill ${health?.ldap?.configured ? "ok" : ""}`}>
          {health?.ldap?.configured ? "LDAP вкл." : "LDAP выкл."}
        </span>
        {health?.disk && (
          <span className={`status-pill ${health.disk.ok ? "" : "bad"}`}>
            {typeof health.disk.free_bytes === "number"
              ? `Диск ${formatBytes(health.disk.free_bytes)} свободно`
              : health.disk.ok
                ? "Диск в порядке"
                : "Мало места на диске"}
            {typeof health.disk.recordings_bytes === "number" && health.disk.recordings_bytes > 0
              ? ` · записи ${formatBytes(health.disk.recordings_bytes)}`
              : ""}
          </span>
        )}
      </div>
      {tab === "users" ? (
        <UsersPanel />
      ) : tab === "ldap" ? (
        <LdapPanel health={health} onChange={onHealth} />
      ) : tab === "audit" ? (
        <AuditPanel />
      ) : (
        <SettingsPanel topic={tab} health={health} onChange={onHealth} />
      )}
      {error && <div className="banner" role="alert">{error}</div>}
    </section>
  );
}


function UsersPanel() {
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [email, setEmail] = useState("");
  const [limit, setLimit] = useState(5);
  const [authMode, setAuthMode] = useState<"local" | "ldap">("local");
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
    setSecret(null);
    setBusy(true);
    try {
      const created = await createUser(email.trim(), limit, authMode);
      if (created.password) setSecret({ email: created.user.email, password: created.password });
      setEmail("");
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось создать пользователя");
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(id: string, userEmail: string) {
    if (!window.confirm(`Удалить ${userEmail} и встречи этого профиля?`)) return;
    setError(null);
    try {
      await deleteUser(id);
      if (secret?.email === userEmail) setSecret(null);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось удалить пользователя");
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

  async function onMode(id: string, mode: "local" | "ldap") {
    setError(null);
    try {
      const next = await setUserAuthMode(id, mode);
      if (next.password) setSecret({ email: next.user.email, password: next.password });
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сменить способ входа");
      await reload();
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
        <div className="choice-grid">
          <button
            type="button"
            className={`choice-card ${authMode === "local" ? "active" : ""}`}
            onClick={() => setAuthMode("local")}
          >
            <strong>Локальный</strong>
            <small>MoM сгенерирует пароль</small>
          </button>
          <button
            type="button"
            className={`choice-card ${authMode === "ldap" ? "active" : ""}`}
            onClick={() => setAuthMode("ldap")}
          >
            <strong>LDAP</strong>
            <small>Пароль из каталога</small>
          </button>
        </div>
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
            {busy ? "Создаю…" : authMode === "ldap" ? "Создать с входом через LDAP" : "Создать и сгенерировать пароль"}
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
                  {" · "}
                  {item.auth_mode === "ldap" ? "LDAP" : "локальный пароль"}
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
              <select
                className="title-input"
                value={item.auth_mode === "ldap" ? "ldap" : "local"}
                onChange={(event) => {
                  const mode = event.target.value === "ldap" ? "ldap" : "local";
                  if (mode !== (item.auth_mode || "local")) void onMode(item.id, mode);
                }}
              >
                <option value="local">Локальный пароль</option>
                <option value="ldap">LDAP</option>
              </select>
              {item.auth_mode !== "ldap" && (
                <button
                  className="ghost"
                  type="button"
                  aria-label={`Сбросить пароль ${item.email}`}
                  onClick={() => void onReset(item.id, item.email)}
                >
                  Сбросить пароль
                </button>
              )}
              <button
                className="ghost"
                type="button"
                aria-label={`Удалить ${item.email}`}
                onClick={() => void onDelete(item.id, item.email)}
              >
                Удалить
              </button>
            </div>
          ))
        )}
      </div>
      {error && <div className="banner" role="alert">{error}</div>}
    </>
  );
}

function LdapPanel({ health, onChange }: { health: Health | null; onChange: (health: Health) => void }) {
  const ldap = health?.ldap;
  const [enabled, setEnabled] = useState(Boolean(ldap?.enabled));
  const [url, setUrl] = useState(ldap?.url || "");
  const [bindDn, setBindDn] = useState(ldap?.bind_dn || "");
  const [bindPassword, setBindPassword] = useState("");
  const [baseDn, setBaseDn] = useState(ldap?.base_dn || "");
  const [userFilter, setUserFilter] = useState(ldap?.user_filter || "(mail={username})");
  const [starttls, setStarttls] = useState(Boolean(ldap?.starttls));
  const [tlsVerify, setTlsVerify] = useState(ldap?.tls_verify !== false);
  const [emailAttr, setEmailAttr] = useState(ldap?.email_attr || "mail");
  const [probe, setProbe] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!health?.ldap) return;
    setEnabled(Boolean(health.ldap.enabled));
    setUrl(health.ldap.url || "");
    setBindDn(health.ldap.bind_dn || "");
    setBaseDn(health.ldap.base_dn || "");
    setUserFilter(health.ldap.user_filter || "(mail={username})");
    setStarttls(Boolean(health.ldap.starttls));
    setTlsVerify(health.ldap.tls_verify !== false);
    setEmailAttr(health.ldap.email_attr || "mail");
  }, [health]);

  return (
    <form
      className="key-box"
      onSubmit={(event) => {
        event.preventDefault();
        setError(null);
        setNote(null);
        setBusy("save");
        void saveLdap({
          enabled,
          url: url.trim(),
          bind_dn: bindDn.trim(),
          bind_password: bindPassword,
          base_dn: baseDn.trim(),
          user_filter: userFilter.trim() || "(mail={username})",
          starttls,
          tls_verify: tlsVerify,
          email_attr: emailAttr.trim() || "mail",
        })
          .then((next) => {
            setBindPassword("");
            onChange(next);
            setNote("Настройки каталога сохранены");
          })
          .catch((err: Error) => setError(err.message))
          .finally(() => setBusy(null));
      }}
    >
      <p>
        {ldap?.configured
          ? "Каталог включён. Пользователю можно выбрать вход через LDAP."
          : "Пока каталог выключен, новые пользователи входят только локальным паролем."}
      </p>
      <div className="field-stack">
        <label className="field-check">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          Включить вход через каталог
        </label>
        <label className="field-label">
          Адрес
          <input
            className="title-input"
            placeholder="ldaps://ldap.example.com:636"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Служебная учётная запись (bind DN)
          <input
            className="title-input"
            placeholder="cn=mom,ou=services,dc=example,dc=com"
            value={bindDn}
            onChange={(event) => setBindDn(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Пароль bind
          <input
            className="title-input"
            type="password"
            autoComplete="new-password"
            placeholder={ldap?.has_password ? "Задан, оставьте пустым чтобы не менять" : "Пароль"}
            value={bindPassword}
            onChange={(event) => setBindPassword(event.target.value)}
          />
        </label>
        <label className="field-label">
          База поиска
          <input
            className="title-input"
            placeholder="ou=people,dc=example,dc=com"
            value={baseDn}
            onChange={(event) => setBaseDn(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Фильтр
          <input
            className="title-input"
            placeholder="(mail={username})"
            value={userFilter}
            onChange={(event) => setUserFilter(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Атрибут email
          <input className="title-input" value={emailAttr} onChange={(event) => setEmailAttr(event.target.value)} />
        </label>
        <label className="field-check">
          <input type="checkbox" checked={starttls} onChange={(event) => setStarttls(event.target.checked)} />
          STARTTLS (для ldap://, не для ldaps://)
        </label>
        <label className="field-check">
          <input type="checkbox" checked={tlsVerify} onChange={(event) => setTlsVerify(event.target.checked)} />
          Проверять сертификат
        </label>
        <label className="field-label">
          Проверить поиск по email
          <input
            className="title-input"
            type="email"
            placeholder="user@example.com"
            value={probe}
            onChange={(event) => setProbe(event.target.value)}
          />
        </label>
      </div>
      <div className="composer-row">
        <button className="primary" type="submit" disabled={busy !== null}>
          {busy === "save" ? "Сохраняю…" : "Сохранить каталог"}
        </button>
        <button
          className="ghost"
          type="button"
          disabled={busy !== null}
          onClick={() => {
            setError(null);
            setNote(null);
            setBusy("test");
            void testLdap(probe.trim())
              .then((result) =>
                setNote(
                  result.entries
                    ? `Найдена одна запись${result.mail ? `: ${result.mail}` : ""}`
                    : "Служебная учётная запись подключилась",
                ),
              )
              .catch((err: Error) => setError(err.message))
              .finally(() => setBusy(null));
          }}
        >
          {busy === "test" ? "Проверяю…" : "Проверить LDAP"}
        </button>
      </div>
      {note && <p>{note}</p>}
      {error && <div className="banner" role="alert">{error}</div>}
    </form>
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
  if (tab === "ldap") {
    return {
      title: "Каталог",
      lead: "LDAP или LDAPS для выбранных пользователей. Локальные пароли при этом остаются.",
    };
  }
  if (tab === "theme") {
    return {
      title: "Вид",
      lead: "Тема интерфейса по умолчанию для всех пользователей.",
    };
  }
  if (tab === "audit") {
    return {
      title: "Журнал",
      lead: "Входы, пользователи, ключ, каталог, удаление встреч, спикеры и отправка протокола.",
    };
  }
  return {
    title: "Пользователи",
    lead: "Профиль заводится вручную. Локальный вход получает сгенерированный пароль, LDAP берёт пароль из каталога.",
  };
}

const AUDIT_LABELS: Record<string, string> = {
  "user.create": "Создан пользователь",
  "user.delete": "Удалён пользователь",
  "user.auth": "Сменён способ входа",
  "user.password": "Сброшен пароль",
  "user.limit": "Изменён архив",
  "ldap.save": "Изменён каталог",
  "meeting.delete": "Удалена встреча",
  "speaker.rename": "Переименован спикер",
  "speaker.reassign": "Реплики перенесены",
  "email.send": "Протокол отправлен",
  "email.open": "Открыт черновик письма",
  "email.fail": "Письмо не ушло",
  "admin.setup": "Создан администратор",
  "admin.login": "Вход администратора",
  "admin.login.fail": "Неудачный вход администратора",
  "user.login": "Вход пользователя",
  "user.login.fail": "Неудачный вход",
  "key.save": "Сохранён ключ",
  "key.delete": "Ключ удалён",
  "models.save": "Сменены модели",
  "smtp.save": "Изменена почта",
};

function AuditPanel() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  function load() {
    setError(null);
    listAudit()
      .then(setEvents)
      .catch((err: Error) => {
        setEvents([]);
        setError(err.message);
      });
  }

  useEffect(load, []);

  return (
    <div className="audit-panel">
      <div className="actions-row">
        <button className="ghost" type="button" onClick={load}>
          Обновить
        </button>
      </div>
      {error && <div className="banner" role="alert">{error}</div>}
      {events === null ? (
        <p>Загружаю журнал…</p>
      ) : events.length === 0 ? (
        <p>Пока пусто. Здесь появятся создание пользователей, смена каталога, удаление встреч, правка спикеров и отправка почты.</p>
      ) : (
        <div className="audit-list">
          {events.map((event) => (
            <article className="audit-row" key={event.id}>
              <time dateTime={event.at}>{formatDate(event.at)}</time>
              <div>
                <strong>{AUDIT_LABELS[event.action] || event.action}</strong>
                <span>{event.actor}</span>
                {(event.detail || event.target) && (
                  <small>
                    {[event.detail, event.target].filter(Boolean).join(" · ")}
                  </small>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}


function SettingsPanel({
  topic,
  health,
  onChange,
}: {
  topic: Exclude<AdminTab, "users" | "ldap" | "audit">;
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
            {openai?.denied_models && openai.denied_models.length > 0 && (
              <ul>
                {openai.denied_models.map((name) => (
                  <li key={name}>Недоступна: {name}</li>
                ))}
              </ul>
            )}
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
            берите Whisper или GigaAM. Кто говорил, размечается отдельно: в .env нужен HF_TOKEN и пакет из
            requirements-diarize.txt. Имена спикеров потом правятся во вкладке «Транскрипт».
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
                  Пакеты и код модели уже в приложении. При первой расшифровке с Hugging Face скачиваются только веса.
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

      {error && <div className="banner" role="alert">{error}</div>}
    </>
  );
}
