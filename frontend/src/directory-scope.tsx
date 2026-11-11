import { useEffect, useRef, useState } from "react";
import { getDirectoryScope, saveDirectoryScope, browseDirectory, type DirectoryScope, type DirectoryArea } from "./api";

export function DirectoryScopePanel({ onSaved }: { onSaved?: () => void } = {}) {
  const [value, setValue] = useState<DirectoryScope | null>(null);
  const [items, setItems] = useState<DirectoryArea[]>([]);
  const [path, setPath] = useState<DirectoryArea[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [manual, setManual] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [warning, setWarning] = useState("");
  const request = useRef(0);
  useEffect(() => {
    let active = true;
    getDirectoryScope().then((next) => { if (active) setValue(next); })
      .catch((err: Error) => { if (active) setError(err.message); });
    return () => { active = false; request.current += 1; };
  }, []);
  async function browse(nextPath: DirectoryArea[], more = false) {
    const id = ++request.current;
    setBusy(true); setError("");
    try {
      const result = await browseDirectory(nextPath[nextPath.length - 1]?.dn || "", more ? cursor || "" : "");
      if (request.current !== id) return;
      setItems((previous) => {
        const all = more ? [...previous, ...result.items] : result.items;
        return [...new Map(all.map((item) => [item.dn.toLowerCase(), item])).values()];
      });
      setPath(nextPath); setCursor(result.next_cursor); setWarning(result.warning || ""); setLoaded(true);
    } catch (err) { if (request.current === id) setError(err instanceof Error ? err.message : "Не удалось загрузить области"); }
    finally { if (request.current === id) setBusy(false); }
  }
  function select(dn: string, checked: boolean) {
    if (!value) return;
    setNote("");
    const bases = value.bases.filter((item) => item.toLowerCase() !== dn.toLowerCase());
    if (checked) bases.push(dn);
    setValue({ ...value, mode: "selected", bases });
  }
  async function save() {
    if (!value) return;
    setSaving(true); setError(""); setNote("");
    try { setValue(await saveDirectoryScope(value)); setNote("Области сохранены. Обновите сотрудников кнопкой «Синхронизировать AD» ниже в разделе «Каталог»."); onSaved?.(); }
    catch (err) { setError(err instanceof Error ? err.message : "Не удалось сохранить"); }
    finally { setSaving(false); }
  }
  return <section className="directory-scopes">
    <h2>Домены и OU для синхронизации и поиска</h2>
    <p>Выбранные области ограничивают выгрузку кэша, подсказки спикеров и поиск людей для добавления в MoM. Настройки входа через LDAP не меняются.
      Пользователи, уже добавленные в MoM, остаются в подсказках.</p>
    <p className="progress-hint">Подключение настраивается в разделе «Каталог». Загружается только структура доступного каталога, без списка сотрудников.
      Выбор области включает все вложенные подразделения. Если внутри есть подрядчики, выберите только нужные дочерние OU.</p>
    {error && <div className="banner" role="alert">{error}</div>}
    {!value && !error && <p>Загружаю настройки поиска…</p>}
    {value && <>
      {!value.connection_matches && value.mode === "selected" && <p className="banner">Подключение к каталогу изменено.
        Поиск по AD приостановлен: проверьте выбранные области и сохраните их для нового подключения.</p>}
      <fieldset disabled={saving} className="scope-options">
        <legend>Где искать</legend>
        <label><input type="radio" name="scope-mode" checked={value.mode === "all"}
          onChange={() => { setNote(""); setValue({ ...value, mode: "all" }); }} /> Во всей настроенной базе</label>
        <label><input type="radio" name="scope-mode" checked={value.mode === "selected"}
          onChange={() => { setNote(""); setValue({ ...value, mode: "selected" }); }} /> Только в выбранных областях</label>
        {value.mode === "selected" && value.bases.length === 0 && <p>Области не выбраны — подсказки из AD будут отключены.</p>}
        <div className="composer-row">
          <button className="ghost" type="button" disabled={busy} onClick={() => void browse([])}>Загрузить домены и OU</button>
          {path.length > 0 && <button className="ghost" type="button" disabled={busy}
            onClick={() => void browse(path.slice(0, -1))}>На уровень выше</button>}
        </div>
        {busy && <p role="status">Загружаю области…</p>}
        {loaded && <>
          <p className="scope-dn">{path[path.length - 1]?.dn || "Доступные корневые области"}</p>
          {warning && <p className="progress-hint">{warning}</p>}
          <ul className="scope-list">{items.map((item) => <li key={item.dn}>
            <label><input type="checkbox" checked={value.bases.some((dn) => dn.toLowerCase() === item.dn.toLowerCase())}
              onChange={(event) => select(item.dn, event.target.checked)} />
              <span><strong>{item.name}</strong><small>{item.dn}</small></span></label>
            <button className="ghost" type="button" disabled={busy} onClick={() => void browse([...path, item])}
              aria-label={`Открыть подразделения ${item.name}`}>Подразделения</button>
          </li>)}</ul>
          {!items.length && !busy && <p>Вложенных областей не найдено или они недоступны этой учётной записи.</p>}
          {cursor && <button className="ghost" type="button" disabled={busy} onClick={() => void browse(path, true)}>Загрузить ещё</button>}
        </>}
        <label className="field-label">Добавить область вручную (DN)
          <input className="title-input" value={manual} placeholder="OU=Employees,DC=example,DC=com"
            onChange={(event) => setManual(event.target.value)} />
        </label>
        <button className="ghost" type="button" disabled={!manual.trim()} onClick={() => { select(manual.trim(), true); setManual(""); }}>Добавить область</button>
        <p><strong>Выбранные области: {value.bases.length}</strong>{value.mode === "all" ? " (не применяются в режиме всей базы)" : ""}</p>
        <ul className="scope-list">{value.bases.map((dn) => <li key={dn}><span className="scope-dn">{dn}</span>
          <button className="ghost" type="button" onClick={() => select(dn, false)} aria-label={`Убрать область ${dn}`}>Убрать</button></li>)}</ul>
      </fieldset>
      <button className="primary" type="button" disabled={saving || value.bases.length > 32} onClick={() => void save()}>
        {saving ? "Сохраняю…" : "Сохранить области поиска"}</button>
      {value.bases.length > 32 && <p role="alert">Выберите не больше 32 областей.</p>}
      {note && <p role="status">{note}</p>}
    </>}
  </section>;
}
