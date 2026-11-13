import { useState } from "react";
import { getSettings, loginAdmin, logoutAdmin, setupAdmin } from "./api";
import { connectionClass, connectionLabel, formatBytes } from "./format";
import { MonitoringPanel } from "./monitoring";
import { CorporateContextPanel } from "./corporate-context";
import type { AdminTab, AuthStatus, Health } from "./types";

import { UsersPanel } from "./admin-users";
import { LdapPanel } from "./admin-directory";
import { AuditPanel } from "./admin-audit";
import { SettingsPanel } from "./admin-settings";
import { PromptsPanel } from "./admin-prompts";
import { ExchangePanel } from "./admin-exchange";

export const ADMIN_TABS: { id: AdminTab; label: string }[] = [
  { id: "monitoring", label: "Мониторинг" },
  { id: "llm", label: "LLM" },
  { id: "prompts", label: "Промпты" },
  { id: "asr", label: "Расшифровка" },
  { id: "context", label: "Контекст" },
  { id: "queue", label: "Очередь" },
  { id: "smtp", label: "Почта" },
  { id: "ldap", label: "Каталог" },
  { id: "exchange", label: "Exchange" },
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
      {tab !== "monitoring" && <div className="status-pills" aria-label="Сводка">
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
      </div>}
      {tab === "monitoring" ? (
        <MonitoringPanel />
      ) : tab === "context" ? (
        <CorporateContextPanel />
      ) : tab === "users" ? (
        <UsersPanel ldapConfigured={Boolean(health?.ldap?.configured)} />
      ) : tab === "ldap" ? (
        <LdapPanel health={health} onChange={onHealth} />
      ) : tab === "exchange" ? (
        <ExchangePanel />
      ) : tab === "prompts" ? (
        <PromptsPanel />
      ) : tab === "audit" ? (
        <AuditPanel />
      ) : (
        <SettingsPanel topic={tab} health={health} onChange={onHealth} />
      )}
      {error && <div className="banner" role="alert">{error}</div>}
    </section>
  );
}


function adminHeading(tab: AdminTab): { title: string; lead: string } {
  if (tab === "exchange") return { title: "Календарь Exchange", lead: "События корпоративного календаря на стартовом экране пользователя." };
  if (tab === "context") return { title: "Корпоративный контекст", lead: "Корпоративные глоссарии и сотрудники AD для автоматического подбора подсказок к записям." };
  if (tab === "monitoring") return { title: "Мониторинг", lead: "Текущая обработка встреч и нагрузка на MoM." };
  if (tab === "prompts") return { title: "Промпты и проверка имён", lead: "Инструкции, которые получает LLM при обработке встреч." };
  if (tab === "llm") {
    return {
      title: "LLM и ключ",
      lead: "Ключ Qwen, OpenAI, Gemini или OpenRouter и модель чата. Она пишет саммари и протокол.",
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
      lead: "Подключение к AD, поиск спикеров и обновление локального кэша сотрудников.",
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
      lead: "Последние 20, 40 или 100 событий. CSV и XLSX выгружают весь журнал.",
    };
  }
  return {
    title: "Пользователи",
    lead: "Профиль заводится вручную. Локальный вход получает сгенерированный пароль, LDAP берёт пароль из каталога.",
  };
}
