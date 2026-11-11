import { useEffect, useState } from "react";
import { getDirectoryCache, saveDirectorySchedule, syncCorporateDirectory, saveDirectoryPolicy, saveSpeakerSearchMode,
  type CorporateContext } from "./api";
import { formatDate } from "./format";
import { DirectoryScopePanel } from "./directory-scope";

const AD_FIELDS = [{key: "job_title", label: "Должность (title)"},
  {key: "department", label: "Подразделение (department)"}, {key: "surname", label: "Фамилия для распознавания (sn)"}];

export function DirectoryCachePanel() {
  const [data, setData] = useState<CorporateContext | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [syncTime, setSyncTime] = useState("03:00");
  const [fields, setFields] = useState(AD_FIELDS.map(field => field.key));
  const [domains, setDomains] = useState("");
  const [showScopes, setShowScopes] = useState(false);
  const savedPolicy = JSON.stringify(data?.directory_policy);
  useEffect(() => {
    if (!savedPolicy) return;
    const policy = JSON.parse(savedPolicy);
    setFields(policy.fields); setDomains(policy.domains.join("\n"));
  }, [savedPolicy]);
  useEffect(() => { if (data?.sync_time) setSyncTime(data.sync_time); }, [data?.sync_time]);

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    let timer: number;
    const refresh = () => {
      getDirectoryCache(controller.signal).then(next => {
        if (!controller.signal.aborted) {
          setData(next); setLoadError(null);
          if (next.syncing) timer = window.setTimeout(refresh, 2000);
          else timer = window.setTimeout(refresh, 30000);
        }
      }).catch(err => {
        if (!controller.signal.aborted) { setLoadError(err.message); timer = window.setTimeout(refresh, 5000); }
      });
    };
    timer = window.setTimeout(refresh, 0);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [busy]);

  async function update(action: () => Promise<CorporateContext>, success: string) {
    setBusy(true); setError(null); setMessage("");
    try {
      setData(await action());
      setMessage(success);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось обновить настройки каталога");
    } finally { setBusy(false); }
  }

  return <div className="context-panel" aria-busy={busy}>
    {(error || loadError) && <div className="banner" role="alert">{error || loadError}</div>}
    {message && <p role="status">{message}</p>}
    {!data && !loadError && <p role="status">Загружаю настройки кэша AD…</p>}
    {data && <>
      <section className="key-box">
        <h2>Поиск спикеров и локальный кэш AD</h2>
        <fieldset className="speaker-search-mode" disabled={busy}>
          <legend>Источник поиска спикеров</legend>
          <label className="context-toggle"><input type="radio" name="speaker-search-mode" checked={data.search_mode === "live"}
            onChange={() => void update(() => saveSpeakerSearchMode("live"), "Источник поиска: живой AD")} />
            Живой AD (по умолчанию)</label>
          <p>При вводе ФИО запрос идёт в корпоративный AD. Результаты актуальны на момент поиска; скорость зависит от AD.</p>
          <label className="context-toggle"><input type="radio" name="speaker-search-mode" checked={data.search_mode === "cache"}
            disabled={!data.search_cache_ready} aria-describedby="speaker-cache-status"
            onChange={() => void update(() => saveSpeakerSearchMode("cache"), "Источник поиска: локальный кэш AD")} />
            Локальный кэш AD</label>
          <p>Быстрый поиск по последней успешной синхронизации, без запросов в AD при вводе имени.</p>
          <p id="speaker-cache-status">{data.search_cache_ready
            ? `Кэш готов к использованию. Сотрудников: ${data.people_count}.`
            : "Для включения кэша сначала успешно синхронизируйте AD с текущими настройками."}</p>
          <p>Выбор действует для всех пользователей со следующего поискового запроса. Синхронизация не переключает режим автоматически.
            При ошибке поиска источник остаётся выбранным; переключить его можно здесь.</p>
        </fieldset>
        <p>Использование сотрудников в распознавании включается отдельно на вкладке «Контекст».</p>
        <p>Сотрудников в кэше — {data.people_count}. {data.synced_at && `Обновлено ${formatDate(data.synced_at)}.`}</p>
        {!data.directory_current && <p className="health-warn">Справочник нужно обновить. Сохраните ограничения и нажмите «Синхронизировать AD».</p>}
        <form className="directory-policy" onSubmit={event => {
          event.preventDefault();
          void update(() => saveDirectoryPolicy({fields, domains: domains.split(/[\s,;]+/).filter(Boolean)}),
            "Правила сохранены. Нажмите «Синхронизировать AD», чтобы обновить справочник.");
        }}>
          <fieldset disabled={busy || data.syncing}>
            <legend>Какие поля загружать</legend>
            <p>ФИО (displayName / cn) и email обязательны. Дополнительные поля можно отключить; остальные атрибуты AD не загружаются.</p>
            {AD_FIELDS.map(field => <label className="context-toggle" key={field.key}>
              <input type="checkbox" checked={fields.includes(field.key)} onChange={event => setFields(current =>
                event.target.checked ? [...current, field.key] : current.filter(key => key !== field.key))} />{field.label}
            </label>)}
            <label className="field-label">Разрешённые почтовые домены
              <textarea className="title-input" rows={2} value={domains} placeholder="company.ru, company.com"
                onChange={event => setDomains(event.target.value)} aria-describedby="ad-domain-hint" />
            </label>
            <p id="ad-domain-hint">Пусто — любой почтовый домен. Перечислите через запятую или с новой строки.
              Например, company.ru включает только адреса @company.ru, без поддоменов. Сотрудник должен также входить в выбранную OU.</p>
            <button className="ghost" type="submit">Сохранить поля и домены</button>
          </fieldset>
        </form>
        <button className="ghost" type="button" aria-expanded={showScopes} onClick={() => setShowScopes(value => !value)}>
          {showScopes ? "Скрыть выбор OU" : "Выбрать домены AD и OU"}</button>
        {showScopes && <DirectoryScopePanel onSaved={() => {
          void update(() => getDirectoryCache(), "Области сохранены. Нажмите «Синхронизировать AD».");
        }} />}
        <DirectorySyncStatus data={data} />
        <label className="context-toggle"><input type="checkbox" checked={data.sync_ad} disabled={busy}
          onChange={event => void update(() => saveDirectorySchedule({sync_ad: event.target.checked}), "Настройки сохранены")} />
          Обновлять AD автоматически раз в сутки</label>
        <form className="context-schedule" onSubmit={event => {
          event.preventDefault();
          void update(() => saveDirectorySchedule({sync_time: syncTime}), "Время синхронизации сохранено");
        }}>
          <label>Время запуска <input type="time" required value={syncTime} disabled={busy}
            onChange={event => setSyncTime(event.target.value)} aria-describedby="ad-sync-clock" /></label>
          <button type="submit" className="ghost" disabled={busy || !syncTime || syncTime === data.sync_time}>Сохранить время</button>
        </form>
        <p id="ad-sync-clock">Ежедневно по московскому времени (UTC+3), независимо от времени на сервере.</p>
        {data.sync_ad && data.next_sync_at && !data.syncing && <p>Ближайший запуск: {new Intl.DateTimeFormat("ru-RU", {
          timeZone: "Europe/Moscow", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit",
        }).format(new Date(data.next_sync_at))} (МСК).{data.sync_error && " Повтор после ошибки."}</p>}
        {!data.sync_ad && <p>Автоматическая синхронизация выключена. Время сохранится для следующего включения.</p>}
        <div className="composer-row">
          <button type="button" className="ghost" disabled={busy || data.syncing || !data.ldap_configured}
            onClick={() => void update(syncCorporateDirectory, "Обновление запрошено. Можно закрыть вкладку — задача продолжится на сервере.")}>
            {data.syncing ? "Синхронизация выполняется…" : "Синхронизировать AD"}</button>
        </div>
        {!data.ldap_configured && <p>Настройте подключение в разделе «Каталог».</p>}
      </section>
    </>}
  </div>;
}

function DirectorySyncStatus({data}: {data: CorporateContext}) {
  const progress = data.sync_progress;
  const phase = progress?.phase;
  const label = data.sync_error ? "Обновление не завершено"
    : progress?.status === "queued" ? "Ожидает запуска"
    : phase === "connecting" ? "Подключение к AD"
    : progress?.status === "succeeded" ? "Кэш обновлён"
    : data.syncing ? "Получение сотрудников из AD" : "Последнее обновление";
  return <div className="directory-sync-status">
    {data.syncing && <progress aria-label="Обновление кэша AD" />}
    {progress && progress.status !== "idle" && <>
      <p role="status"><strong>{label}</strong> · Получено сотрудников: {progress.received ?? 0}
        {" · "}Страниц: {progress.pages ?? 0}</p>
      {progress.scopes_total !== undefined && <p>Обработано областей: {progress.scopes_done ?? 0} из {progress.scopes_total}</p>}
      {data.syncing && progress.current_base && <p className="scope-dn">Текущая область: {progress.current_base}</p>}
      {progress.started_at && <p>Начало: {formatDate(progress.started_at)}.
        {progress.finished_at && ` Завершение: ${formatDate(progress.finished_at)}.`}</p>}
    </>}
    {data.syncing && <p>Общий объём AD заранее неизвестен. Кэш будет заменён только после полной успешной выгрузки.</p>}
    {data.sync_error && <p className="health-warn" role="alert">{data.sync_error}</p>}
  </div>;
}
