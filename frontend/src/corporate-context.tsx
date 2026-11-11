import { useEffect, useState } from "react";
import { getCorporateContext, importCorporateDictionary, saveCorporateContext,
  type CorporateContext } from "./api";
import { formatDate } from "./format";
export function CorporateContextPanel() {
  const [data, setData] = useState<CorporateContext | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    let timer: number;
    const refresh = () => {
      getCorporateContext(query, controller.signal).then(next => {
        if (!controller.signal.aborted) {
          setData(next); setLoadError(null);
          timer = window.setTimeout(refresh, 30000);
        }
      }).catch(err => {
        if (!controller.signal.aborted) { setLoadError(err.message); timer = window.setTimeout(refresh, 5000); }
      });
    };
    timer = window.setTimeout(refresh, query ? 250 : 0);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [query, busy]);

  async function update(action: () => Promise<CorporateContext>, success: string) {
    setBusy(true); setError(null); setMessage("");
    try {
      setData(await action());
      setQuery("");
      setMessage(success);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось обновить корпоративный контекст");
    } finally { setBusy(false); }
  }

  function downloadExample() {
    const csv = "\ufeffТермин;Варианты;Описание;Подразделение\r\nTrueConf;труконф|тру конф;Система видеоконференций;\r\nSLA;эс эл эй;Соглашение об уровне обслуживания;ИТ\r\n";
    const url = URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"}));
    const link = document.createElement("a");
    link.href = url; link.download = "mom-corporate-dictionary.csv"; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return <div className="context-panel" aria-busy={busy}>
    {(error || loadError) && <div className="banner" role="alert">{error || loadError}</div>}
    {message && <p role="status">{message}</p>}
    {!data && !loadError && <p role="status">Загружаю корпоративный контекст…</p>}
    {data && <>
      <label className="context-toggle"><input type="checkbox" checked={data.enabled} disabled={busy}
        onChange={event => void update(() => saveCorporateContext(event.target.checked, data.use_ad), "Настройки сохранены")} />
        Использовать корпоративный контекст при обработке новых записей</label>
      <section className="key-box">
        <h2>Сотрудники из AD</h2>
        <label className="context-toggle"><input type="checkbox" checked={data.use_ad} disabled={busy}
          onChange={event => void update(() => saveCorporateContext(data.enabled, event.target.checked), "Настройки сохранены")} />
          Использовать сотрудников AD в контексте распознавания</label>
        <p>ФИО, должности и подразделения из последнего полного кэша помогают распознавать имена и учитывать корпоративный контекст.</p>
        <p>Сотрудников в справочнике — {data.people_count}. {data.synced_at && `Обновлено ${formatDate(data.synced_at)}.`}</p>
        <p>Подключение к AD, поиск спикеров, состав выгрузки и расписание обновления настраиваются на вкладке «Каталог».</p>
        {!data.directory_current && <p className="health-warn">Справочник нужно обновить на вкладке «Каталог».</p>}
      </section>
      <section className="key-box">
        <h2>Корпоративный словарь</h2>
        <p>Excel (.xlsx) или CSV до 2 МБ, до 5000 строк. Загрузка заменяет предыдущий словарь целиком.</p>
        <p>Обязательный столбец «Термин». Дополнительные столбцы «Варианты», «Описание», «Подразделение».
          Варианты разделяйте знаком |. Пустое подразделение означает общий термин.</p>
        <p>Терминов — {data.term_count}. {data.imported_at && `Загружено ${formatDate(data.imported_at)}.`}</p>
        <form onSubmit={event => { event.preventDefault(); if (file) void update(() => importCorporateDictionary(file), "Словарь обновлён"); }}>
          <label>Файл словаря <input type="file" accept=".xlsx,.csv" disabled={busy}
            onChange={event => setFile(event.target.files?.[0] || null)} /></label>
          <div className="composer-row">
            <button type="submit" className="primary" disabled={busy || !file}>{busy ? "Обновляю…" : "Загрузить словарь"}</button>
            <button type="button" className="ghost" onClick={downloadExample}>Скачать образец CSV</button>
          </div>
        </form>
        <label className="context-search">Поиск по словарю
          <input type="search" className="title-input" value={query} onChange={event => setQuery(event.target.value)} />
        </label>
        <p>Показано {data.terms.length} из {data.matched_terms}</p>
        <div className="context-table"><table aria-label="Корпоративный словарь">
          <thead><tr><th>Термин</th><th>Варианты</th><th>Описание</th><th>Подразделение</th></tr></thead>
          <tbody>{data.terms.map(entry => <tr key={`${entry.term}-${entry.department}`}>
            <td>{entry.term}</td><td>{entry.aliases.join(", ")}</td><td>{entry.description}</td><td>{entry.department || "Общее"}</td>
          </tr>)}</tbody>
        </table></div>
      </section>
    </>}
  </div>;
}
