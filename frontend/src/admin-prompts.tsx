import { useEffect, useState } from "react";
import { getPrompts, savePrompts, type PromptSettings } from "./api";

export function PromptsPanel() {
  const [settings, setSettings] = useState<PromptSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  useEffect(() => {
    let active = true;
    getPrompts().then((value) => { if (active) setSettings(value); })
      .catch((err) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, []);
  async function save() {
    if (!settings) return;
    setBusy(true); setError(null); setNote("");
    try {
      const next = await savePrompts(settings);
      setSettings(next); setNote(`Сохранено · версия ${next.version}`);
    } catch (err) { setError(err instanceof Error ? err.message : "Не удалось сохранить"); }
    finally { setBusy(false); }
  }
  return <div className="prompt-settings">
    {error && <div className="banner" role="alert">{error}</div>}
    {!settings && !error && <p>Загружаю промпты…</p>}
    {settings && <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <p className="progress-hint">Изменения применятся к следующим запускам обработки. Готовые встречи автоматически не пересчитываются.
        Сохраните JSON-схемы в инструкциях: по ним приложение читает ответы модели.</p>
      <label className="prompt-toggle"><input type="checkbox" checked={settings.name_review_enabled} disabled={busy}
        onChange={(event) => { setNote(""); setSettings({ ...settings, name_review_enabled: event.target.checked }); }} />
        Проверять имена после разметки спикеров, перед составлением протокола</label>
      {(["protocol_prompt", "name_review_prompt"] as const).map((key) => <div className="prompt-field" key={key}>
        <label htmlFor={key}>{key === "protocol_prompt" ? "Системный промпт протокола" : "Системный промпт проверки имён"}</label>
        <textarea id={key} value={settings[key]} rows={16} maxLength={30000} disabled={busy} required
          onChange={(event) => { setNote(""); setSettings({ ...settings, [key]: event.target.value }); }} />
        <button type="button" className="ghost" disabled={busy}
          onClick={() => { setNote("Стандартный текст подставлен. Нажмите «Сохранить» для применения.");
            setSettings({ ...settings, [key]: settings.defaults[key] }); }}>Вернуть стандартный текст</button>
      </div>)}
      <button className="primary" type="submit" disabled={busy || !settings.protocol_prompt.trim() || !settings.name_review_prompt.trim()}>
        {busy ? "Сохраняю…" : "Сохранить"}</button>
      {note && <p role="status">{note}</p>}
    </form>}
  </div>;
}
