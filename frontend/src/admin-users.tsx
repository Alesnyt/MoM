import { useEffect, useState } from "react";
import { createUser, listDirectory, type DirectoryPerson, listUsers, deleteUser, resetUserPassword, setUserAuthMode, updateUserLimit } from "./api";
import type { PlatformUser } from "./types";

export function UsersPanel({ ldapConfigured }: { ldapConfigured: boolean }) {
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [email, setEmail] = useState("");
  const [limit, setLimit] = useState(5);
  const [authMode, setAuthMode] = useState<"local" | "ldap">("local");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [secret, setSecret] = useState<{ email: string; password: string } | null>(null);
  const [directory, setDirectory] = useState<DirectoryPerson[]>([]);
  const [directoryState, setDirectoryState] = useState<"idle" | "loading" | "empty" | "error">("idle");
  const [directoryNote, setDirectoryNote] = useState<string | null>(null);

  async function reload() {
    setUsers(await listUsers());
  }

  useEffect(() => {
    reload().catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    if (authMode !== "ldap" || !ldapConfigured) {
      setDirectory([]);
      setDirectoryState("idle");
      setDirectoryNote(null);
      return;
    }
    const query = email.trim();
    if (query.length < 2) {
      setDirectory([]);
      setDirectoryState("idle");
      setDirectoryNote(null);
      return;
    }
    let cancelled = false;
    setDirectoryState("loading");
    const timer = window.setTimeout(() => {
      listDirectory(query)
        .then((found) => {
          if (cancelled) return;
          const taken = new Set(users.map((item) => item.email.toLowerCase()));
          const fresh = found.people.filter((item) => item.email && !taken.has(item.email.toLowerCase()));
          setDirectory(fresh);
          setDirectoryNote(found.error || null);
          setDirectoryState(found.error ? "error" : fresh.length ? "idle" : "empty");
        })
        .catch((err: Error) => {
          if (cancelled) return;
          setDirectory([]);
          setDirectoryNote(err.message);
          setDirectoryState("error");
        });
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [authMode, email, ldapConfigured, users]);

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
          type={authMode === "ldap" ? "text" : "email"}
          inputMode={authMode === "ldap" ? "email" : undefined}
          autoComplete="off"
          placeholder={authMode === "ldap" ? "Имя или email из каталога" : "email@company.com"}
          aria-label={authMode === "ldap" ? "Сотрудник из каталога" : "Email"}
          list={authMode === "ldap" ? "ldap-people" : undefined}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        {authMode === "ldap" && (
          <>
            <datalist id="ldap-people">
              {directory.map((person) => (
                <option key={person.email} value={person.email}>
                  {person.name !== person.email ? person.name : person.email}
                </option>
              ))}
            </datalist>
            <p className="progress-hint">
              {!ldapConfigured
                ? "Каталог не подключён. Сначала включите LDAP во вкладке «Каталог»."
                : directoryState === "loading"
                  ? "Ищу в каталоге…"
                  : directoryState === "error"
                    ? directoryNote || "Каталог не ответил. Email можно вписать целиком."
                    : directoryState === "empty"
                      ? "В каталоге никого не нашлось. Email можно вписать целиком."
                      : directory.length
                        ? "Выберите сотрудника из списка или впишите email целиком."
                        : "Начните вводить имя или почту — варианты подтянутся из каталога."}
            </p>
          </>
        )}
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
