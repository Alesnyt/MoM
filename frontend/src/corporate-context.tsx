import { useEffect, useState } from "react";
import { getCorporateContext, saveCorporateContext,
  type CorporateContext } from "./api";
import { GlossariesPanel } from "./glossaries";
import { formatDate } from "./format";
export function CorporateContextPanel() {
  const [data, setData] = useState<CorporateContext | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    let timer: number;
    const refresh = () => {
      getCorporateContext("", controller.signal).then(next => {
        if (!controller.signal.aborted) {
          setData(next); setLoadError(null);
          timer = window.setTimeout(refresh, 30000);
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
      setError(err instanceof Error ? err.message : "Не удалось обновить корпоративный контекст");
    } finally { setBusy(false); }
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
      <GlossariesPanel />
    </>}
  </div>;
}
