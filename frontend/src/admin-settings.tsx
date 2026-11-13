import { useEffect, useRef, useState } from "react";
import { deleteApiKey, saveApiKey, listChatModels, saveModels, saveQueue, saveSmtp, saveTheme, testSmtp, verifyApiKey } from "./api";
import { encodeAsrModel, formatAsrLabel, parseAsrModel, WHISPER_SIZES, type AsrEngine } from "./asr";
import { connectionClass, connectionLabel, formatDate } from "./format";
import type { AdminTab, Health } from "./types";

function ChatModelPicker({ models, value, onChange }: {
  models: string[];
  value: string;
  onChange: (model: string) => void;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const matches = needle ? models.filter((name) => name.toLowerCase().includes(needle)) : models;

  return (
    <div className="field-stack">
      <label className="field-label">
        Поиск модели
        <input
          className="title-input"
          type="search"
          placeholder="Например flash или mini"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.nativeEvent.isComposing) event.preventDefault();
          }}
        />
      </label>
      <label className="field-label">
        Модель чата
        <select
          className="title-input"
          value={value}
          disabled={matches.length === 0}
          onChange={(event) => onChange(event.target.value)}
        >
          {!matches.includes(value) && <option value={value} disabled>{value || "Выберите модель"}</option>}
          {matches.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      </label>
      <p className="progress-hint" role="status">
        {matches.length ? `Моделей в списке: ${matches.length}` : "Модели не найдены. Попробуйте другую часть названия."}
      </p>
    </div>
  );
}

export function SettingsPanel({
  topic,
  health,
  onChange,
}: {
  topic: Exclude<AdminTab, "monitoring" | "users" | "ldap" | "audit" | "prompts" | "exchange">;
  health: Health | null;
  onChange: (health: Health) => void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [chatModel, setChatModel] = useState(health?.openai.chat_model || "");
  const [chatCatalog, setChatCatalog] = useState<string[] | null>(null);
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
  const [busy, setBusy] = useState<"save" | "verify" | "verify-key" | "delete" | "models" | "theme" | "smtp" | "smtp-test" | "queue" | null>(null);
  const verification = useRef<AbortController | null>(null);
  const [verificationNote, setVerificationNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const openai = health?.openai;
  const asr = parseAsrModel(asrModel);

  useEffect(() => () => verification.current?.abort(), []);

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

  useEffect(() => {
    if (topic !== "llm" || !health?.openai.configured) {
      setChatCatalog(null);
      return;
    }
    let cancelled = false;
    listChatModels()
      .then((models) => {
        if (!cancelled) setChatCatalog(models);
      })
      .catch(() => {
        if (!cancelled) setChatCatalog([]);
      });
    return () => {
      cancelled = true;
    };
  }, [topic, openai?.configured, openai?.hint, openai?.checked_at]);

  async function run(
    kind: "delete" | "models" | "theme" | "smtp" | "queue",
    action: () => Promise<Health>,
  ) {
    if (busy !== null) return;
    setError(null);
    setVerificationNote("");
    setBusy(kind);
    try {
      const next = await action();
      onChange(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось выполнить действие");
    } finally {
      setBusy(null);
    }
  }

  async function checkConnection(key?: string, model?: string) {
    if (busy !== null || verification.current) return;
    const controller = new AbortController();
    verification.current = controller;
    setError(null);
    setVerificationNote("");
    try {
      if (key !== undefined || model !== undefined) {
        setBusy("save");
        const saved = key !== undefined ? await saveApiKey(key)
          : await saveModels(model!, asrModel.trim() || health?.openai.asr_model || "local-whisper");
        if (controller.signal.aborted) return;
        onChange(saved);
        if (key !== undefined) setApiKey("");
      }
      if (model !== undefined && !openai?.configured) return;
      setBusy(key === undefined ? "verify" : "verify-key");
      const next = await verifyApiKey(controller.signal);
      if (!controller.signal.aborted) onChange(next);
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err instanceof Error ? err.message : "Не удалось проверить подключение");
      }
    } finally {
      if (verification.current === controller && !controller.signal.aborted) {
        verification.current = null;
        setBusy(null);
      }
    }
  }

  function cancelVerification() {
    verification.current?.abort();
    verification.current = null;
    setBusy(null);
    setVerificationNote(busy === "verify-key" ? "Проверка отменена. Ключ сохранён." : "Проверка отменена.");
  }

  const cancelButton = <button className="ghost" type="button" onClick={cancelVerification}>Отменить проверку</button>;

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
              if (apiKey.trim().length >= 20) void checkConnection(apiKey.trim());
            }}
          >
            <p>{openai?.configured ? "Заменить ключ" : "Добавить ключ Qwen, OpenAI, Gemini или OpenRouter"}</p>
            <div className="composer-row">
              <input
                className="title-input"
                type="password"
                autoComplete="off"
                placeholder="sk-ws-…, sk-…, sk-or-…, AIza… или AQ.…"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
              />
              <button className="primary" type="submit" disabled={busy !== null || apiKey.trim().length < 20}>
                {busy === "save" ? "Сохраняю…" : busy === "verify-key" ? "Проверяю…" : "Сохранить и проверить"}
              </button>
              {busy === "verify-key" && cancelButton}
            </div>
          </form>

          <form
            className="key-box"
            onSubmit={(event) => {
              event.preventDefault();
              void checkConnection(undefined, chatModel.trim());
            }}
          >
            <p>Модель, которая собирает протокол.</p>
            {health?.openai.configured && chatCatalog === null ? (
              <label className="field-label">
                Модель чата
                <input className="title-input" value="Загружаю модели этого ключа…" disabled />
              </label>
            ) : chatCatalog?.length ? (
              <ChatModelPicker models={chatCatalog} value={chatModel} onChange={setChatModel} />
            ) : (
              <>
                <label className="field-label">
                  Модель чата
                  <input
                    className="title-input"
                    placeholder="Например gemini-3.8-flash или qwen3.7-plus"
                    value={chatModel}
                    onChange={(event) => setChatModel(event.target.value)}
                  />
                </label>
                <p className="progress-hint">Каталог моделей недоступен — впишите имя вручную.</p>
              </>
            )}
            <div className="composer-row">
              <button className="primary" type="submit" disabled={busy !== null || !chatModel.trim()}>
                {openai?.configured ? "Сохранить и проверить модель" : "Сохранить модель"}
              </button>
            </div>
          </form>

          <div className="pane-actions settings-actions">
            <button
              className="ghost"
              type="button"
              disabled={busy !== null || !openai?.configured}
              onClick={() => void checkConnection()}
            >
              {busy === "verify" ? "Проверяю…" : "Проверить подключение"}
            </button>
            {busy === "verify" && cancelButton}
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
            Сейчас: {formatAsrLabel(openai?.asr_model) || "не задано"}. Token Plan, Gemini и OpenRouter не включают облачный ASR
            в этом приложении — берите Whisper или GigaAM. Кто говорил, размечается отдельно: в .env нужен HF_TOKEN и пакет из
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
                <small>Qwen ASR в текущем подключении возвращает текст без таймкодов. Для автоматической разметки спикеров выберите Whisper или GigaAM.</small>
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
            1 — спокойно для 4–8 ГБ. 2–4, если RAM и CPU позволяют: ffmpeg и облачная модель идут параллельно. Локальный
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

      {topic !== "llm" && (busy === "verify" || busy === "verify-key") && (
        <div className="pane-actions settings-actions">
          <span role="status">Проверяю подключение…</span>
          {cancelButton}
        </div>
      )}
      {verificationNote && <p className="progress-hint" role="status">{verificationNote}</p>}
      {error && <div className="banner" role="alert">{error}</div>}
    </>
  );
}
