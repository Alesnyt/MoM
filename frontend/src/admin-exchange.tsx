import { useEffect, useState } from "react";
import { getExchangeSettings, saveExchangeSettings } from "./calendar-api";
import type { ExchangeSettings } from "./calendar-types";

export function ExchangePanel() {
  const [settings, setSettings] = useState<ExchangeSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    getExchangeSettings(controller.signal).then(setSettings).catch((err: Error) => { if (!controller.signal.aborted) setError(err.message); });
    return () => controller.abort();
  }, []);
  async function save() {
    if (!settings) return;
    setBusy(true); setError(""); setNote("");
    try {
      setSettings(await saveExchangeSettings(settings));
      setNote("Сохранено. При следующем входе через LDAP календарь подключится автоматически. Для локального профиля: «Встречи» → «Новая запись» → подключить календарь справа.");
    } catch (err) { setError(err instanceof Error ? err.message : "Не удалось сохранить Exchange"); }
    finally { setBusy(false); }
  }
  return <div className="exchange-settings">
    <p className="lead">Корпоративный Exchange: события, повестка, приглашённые и переговорки. После входа через LDAP календарь подключается автоматически для этой же учётной записи. Локальные профили подключают календарь вручную.</p>
    {error && <div className="banner" role="alert">{error}</div>}
    {!settings && !error && <p role="status">Загружаю настройки…</p>}
    {settings && <form className="field-stack" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label className="prompt-toggle"><input type="checkbox" checked={settings.enabled} disabled={busy}
        onChange={(event) => { setNote(""); setSettings({ ...settings, enabled: event.target.checked }); }} />Включить календарь Exchange</label>
      <label className="field-label">Адрес OWA или EWS
        <input className="title-input" value={settings.url} type="text" required={settings.enabled} disabled={busy}
          placeholder="https://mail.company.ru/EWS/Exchange.asmx" autoComplete="off"
          onChange={(event) => { setNote(""); setSettings({ ...settings, url: event.target.value }); }} /></label>
      <p className="progress-hint">Можно вставить адрес веб-почты вида https://mail.company.ru/owa. Используется HTTPS с проверкой сертификата.</p>
      <label className="field-label">Способ входа
        <select className="title-input" value={settings.auth_mode} disabled={busy}
          onChange={(event) => { setNote(""); setSettings({ ...settings, auth_mode: event.target.value as "ntlm" | "basic" }); }}>
          <option value="ntlm">Доменная учётная запись (NTLM)</option>
          <option value="basic">Логин и пароль (Basic через HTTPS)</option>
        </select></label>
      <p className="progress-hint">MoM только читает календарь. Письма, вложения и контакты не запрашиваются; события не изменяются. Смена этих настроек отключит текущие подключения календарей.</p>
      <div><button className="primary" disabled={busy || settings.enabled && !settings.url.trim()}>{busy ? "Сохраняю…" : "Сохранить Exchange"}</button></div>
      {note && <p role="status">{note}</p>}
    </form>}
  </div>;
}
