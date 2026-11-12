import {useEffect, useState} from 'react';
import {getGlossaries, previewGlossary, discardGlossaryPreview, queueGlossary, enableGlossary,
  type GlossaryCatalog, type GlossaryPreview, type GlossaryMapping} from './api';
import {formatDate} from './format';

const fields: [keyof GlossaryMapping,string][] = [['term','Термин'],['description','Определение'],
  ['aliases','Синонимы и сокращения'],['department','Область / подразделение'],['replacements','Разрешённые замены в транскрипте']];

export function GlossariesPanel() {
  const [data,setData] = useState<GlossaryCatalog|null>(null);
  const [query,setQuery] = useState(''); const [filter,setFilter] = useState(''); const [offset,setOffset] = useState(0);
  const [target,setTarget] = useState(''); const [file,setFile] = useState<File|null>(null);
  const [preview,setPreview] = useState<GlossaryPreview|null>(null); const [mapping,setMapping] = useState<GlossaryMapping>({});
  const [name,setName] = useState(''); const [busy,setBusy] = useState(false); const [searching,setSearching] = useState(false);
  const [error,setError] = useState(''); const [loadError,setLoadError] = useState(''); const [message,setMessage] = useState('');
  const [reload,setReload] = useState(0); const [fileKey,setFileKey] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: number;
    setSearching(true);
    const refresh = () => getGlossaries(query,filter,offset,controller.signal).then(next => {
      if (controller.signal.aborted) return;
      setData(next); setSearching(false); setLoadError('');
      if(offset>0 && offset>=next.matched_terms)setOffset(0);
      timer = window.setTimeout(refresh,next.jobs.some(j=>j.status==='queued'||j.status==='running')?2000:30000);
    }).catch(err => {
      if (controller.signal.aborted) return;
      setLoadError(err.message); setSearching(false); timer=window.setTimeout(refresh,5000);
    });
    timer=window.setTimeout(refresh,query?250:0);
    return () => {controller.abort();window.clearTimeout(timer);};
  },[query,filter,offset,reload]);

  async function action(work: ()=>Promise<void>) {
    setBusy(true);setError('');setMessage('');
    try {await work();setReload(value=>value+1);} catch(err) {setError(err instanceof Error?err.message:'Не удалось обновить глоссарии');}
    finally {setBusy(false);}
  }
  async function inspect(sheet='') {
    if (!file) return;
    await action(async()=> {
      if (preview) {await discardGlossaryPreview(preview.upload_id);setPreview(null);}
      const next=await previewGlossary(file,target,sheet);
      setPreview(next);setMapping(next.mapping);setName(next.name);
    });
  }
  async function reset() {
    if (preview) await discardGlossaryPreview(preview.upload_id);
    setPreview(null);setFile(null);setFileKey(value=>value+1);setMapping({});setName('');
  }
  function example() {
    const text='\ufeffТермин;Определение;Синонимы;Область;Замены\r\nDWH;Корпоративное хранилище данных;хранилище данных;Данные;\r\nTrueConf;Система видеоконференций;ВКС;ИТ;труконф|тру конф\r\n';
    const url=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));
    const link=document.createElement('a');link.href=url;link.download='mom-glossary.csv';link.click();
    window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  return <section className="key-box glossary-panel">
    <div className="glossary-heading"><div><h2>Корпоративные глоссарии</h2><p>Бизнес, данные и корпоративные термины. Каждый словарь обновляется отдельно.</p></div>
      <button type="button" className="ghost" onClick={example}>Образец CSV</button></div>
    {(error||loadError)&&<div className="banner" role="alert">{error||loadError}</div>}
    {message&&<p role="status">{message}</p>}
    {!data&&!loadError&&<p role="status">Загружаю глоссарии…</p>}
    {data&&<>
      <p>Терминов: {data.term_count.toLocaleString('ru-RU')}. Используется: {data.active_term_count.toLocaleString('ru-RU')}.</p>
      <div className="glossary-list">{data.glossaries.map(g=><article key={g.id} className="glossary-card">
        <label className="context-toggle"><input type="checkbox" checked={g.enabled} disabled={busy}
          onChange={event=>void action(()=>enableGlossary(g.id,event.target.checked))}/><strong>{g.name}</strong></label>
        <p>Терминов: {g.term_count.toLocaleString('ru-RU')} · {g.version?`Версия ${g.version}`:'Ещё не загружен'}{!g.enabled?' · Выключен':''}</p>
        {g.updated_at&&<small>Обновлён {formatDate(g.updated_at)}</small>}
      </article>)}</div>
      {data.jobs.slice(0,5).map(job=><div className={`glossary-job ${job.status==='error'?'health-warn':''}`} key={job.id}
        role={job.status==='error'?'alert':'status'}>
        <strong>{data.glossaries.find(g=>g.id===job.glossary_id)?.name||job.filename}: </strong>
        {job.status==='queued'?'Ожидает обработки':job.status==='running'?'Подготавливаю индекс':job.status==='succeeded'?'Готово':'Ошибка импорта'}
        {` · Обработано: ${job.received.toLocaleString('ru-RU')}`}
        {(job.status==='queued'||job.status==='running')&&<><progress aria-label="Подготовка глоссария"/><small>Можно закрыть страницу. До завершения используется предыдущая версия.</small></>}
        {job.error&&<p>{job.error}</p>}
      </div>)}
    </>}
    <form className="glossary-import" onSubmit={event=>{event.preventDefault();void inspect();}} aria-busy={busy}>
      <h3>Загрузить или обновить</h3>
      <p>Excel (.xlsx) или CSV до 25 МБ. Всего до 100 000 терминов. Первая строка — названия столбцов.</p>
      <div className="glossary-fields">
        <label>Куда загрузить<select value={target} disabled={busy||!!preview} onChange={event=>setTarget(event.target.value)}>
          <option value="">Новый глоссарий</option>{data?.glossaries.map(g=><option value={g.id} key={g.id}>Обновить: {g.name}</option>)}
        </select></label>
        <label>Файл глоссария<input key={fileKey} type="file" accept=".csv,.xlsx" disabled={busy||!!preview}
          onChange={event=>{setFile(event.target.files?.[0]||null);setError('');}}/></label>
      </div>
      {!preview&&<button type="submit" className="primary" disabled={!file||busy}>{busy?'Читаю файл…':'Проверить файл'}</button>}
      {preview&&<>
        <div className="glossary-fields">
          <label>Название глоссария<input value={name} maxLength={120} disabled={busy} onChange={event=>setName(event.target.value)}/></label>
          {preview.sheets.length>1&&<label>Лист Excel<select value={preview.sheet} disabled={busy} onChange={event=>void inspect(event.target.value)}>
            {preview.sheets.map(sheet=><option key={sheet}>{sheet}</option>)}</select></label>}
        </div>
        <p>Выберите соответствие столбцов. Оно сохранится для обновлений этого глоссария.</p>
        <div className="glossary-fields">{fields.map(([key,label])=><label key={key}>{label}{key==='term'?' *':''}
          <select value={mapping[key]||''} disabled={busy} onChange={event=>setMapping(previous=>{
            const next={...previous}; if(event.target.value)next[key]=event.target.value;else delete next[key];return next;
          })}><option value="">{key==='term'?'Выберите столбец':'Не использовать'}</option>
            {preview.columns.map((column,i)=><option key={i} value={column} disabled={!column||preview.columns.filter(c=>c===column).length>1}>{column||`Без названия (${i+1})`}</option>)}</select>
        </label>)}</div>
        <p className="glossary-note">Синонимы помогают искать. Только «Разрешённые замены» меняют написание в транскрипте, если соответствие однозначно. Несколько вариантов разделяйте знаком |.</p>
        <details><summary>Первые строки файла</summary><div className="context-table"><table aria-label="Предпросмотр глоссария">
          <thead><tr>{preview.columns.map((column,i)=><th key={i}>{column}</th>)}</tr></thead>
          <tbody>{preview.sample.map((row,i)=><tr key={i}>{row.map((value,j)=><td key={j}>{value}</td>)}</tr>)}</tbody>
        </table></div></details>
        <div className="composer-row">
          <button type="button" className="primary" disabled={busy||!mapping.term||!name.trim()} onClick={()=>void action(async()=>{
            await queueGlossary(preview,name,mapping);setPreview(null);setFile(null);setFileKey(value=>value+1);
            setMessage('Импорт поставлен в очередь. Прогресс отображается выше.');
          })}>{busy?'Подготавливаю…':target?'Обновить этот глоссарий':'Добавить глоссарий'}</button>
          <button type="button" className="ghost" disabled={busy} onClick={()=>void action(reset)}>Отменить загрузку</button>
        </div>
      </>}
    </form>
    {data&&<>
      <div className="glossary-fields glossary-search">
        <label>Поиск по глоссариям<input type="search" value={query} placeholder="Часть термина, сокращение или определение"
          onChange={event=>{setQuery(event.target.value);setOffset(0);}}/></label>
        <label>Показать словарь<select value={filter} onChange={event=>{setFilter(event.target.value);setOffset(0);}}>
          <option value="">Все глоссарии</option>{data.glossaries.map(g=><option value={g.id} key={g.id}>{g.name}</option>)}</select></label>
      </div>
      <p role="status">{searching?<span className="glossary-searching">Поиск…</span>:`Найдено: ${data.matched_terms.toLocaleString('ru-RU')}`}</p>
      <div className="context-table" aria-busy={searching}><table aria-label="Термины глоссариев">
        <thead><tr><th>Термин / источник</th><th>Определение</th><th>Синонимы</th><th>Замены</th><th>Область</th></tr></thead>
        <tbody>{data.terms.map(entry=><tr key={entry.id}><td><strong>{entry.term}</strong><small>{entry.glossary_name}{!entry.enabled?' · Выключен':''}</small></td>
          <td>{entry.description.length>350?<details><summary>{entry.description.slice(0,150)}…</summary>{entry.description}</details>:entry.description||'—'}</td>
          <td>{entry.aliases.join(', ')||'—'}</td><td>{entry.replacements.join(', ')||'—'}</td><td>{entry.department||'Общее'}</td></tr>)}</tbody>
      </table></div>
      {!data.terms.length&&<p>{query?'Совпадений нет. Попробуйте часть названия или сокращение.':'Загрузите первый глоссарий.'}</p>}
      <div className="composer-row glossary-pages"><button type="button" className="ghost" disabled={!offset||searching} onClick={()=>setOffset(value=>Math.max(0,value-100))}>Назад</button>
        <span>{data.matched_terms?`${offset+1}–${Math.min(offset+100,data.matched_terms)} из ${data.matched_terms}`:'0 терминов'}</span>
        <button type="button" className="ghost" disabled={offset+100>=data.matched_terms||searching} onClick={()=>setOffset(value=>value+100)}>Далее</button></div>
    </>}
  </section>;
}
