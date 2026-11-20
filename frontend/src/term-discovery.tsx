import {useEffect,useState} from 'react';
import {getTermDiscovery,getTermVariants,termDiscoveryAction,rejectPendingTerms,getGlossaries,type TermDiscovery,type TermProposal} from './api';
import {formatDate} from './format';
const states=[['pending','На проверке'],['approved','Подтверждённые'],['deferred','Отложенные'],['rejected','Отклонённые'],['ordinary','Скрытые слова']];
const labels:Record<string,string>={idle:'Ожидает запуска',queued:'В очереди',running:'Анализирую новые реплики',
  paused:'Ожидаю завершения обработки встреч',succeeded:'Прогон завершён',error:'Не удалось завершить анализ'};

export function TermDiscoveryPanel({onBack}:{onBack?:()=>void}) {
  const [data,setData]=useState<TermDiscovery|null>(null),[error,setError]=useState('');
  const [state,setState]=useState('pending'),[offset,setOffset]=useState(0),[reload,setReload]=useState(0),[busy,setBusy]=useState(false);
  const [schedule,setSchedule]=useState<{enabled:boolean;run_time:string;max_batches:number}|null>(null);
  const [confirmAll,setConfirmAll]=useState(false),[notice,setNotice]=useState('');
  useEffect(()=>{
    const controller=new AbortController();let timer:number;
    const refresh=()=>getTermDiscovery(state,offset,controller.signal).then(next=>{
      if(controller.signal.aborted)return;setData(next);setError('');
      if(offset&&offset>=next.total)setOffset(0);
      setSchedule(old=>old||{enabled:next.job.enabled,run_time:next.job.run_time,max_batches:next.job.max_batches});
      timer=window.setTimeout(refresh,['queued','running','paused'].includes(next.job.status)?3000:30000);
    }).catch(err=>{if(!controller.signal.aborted){setError(err.message);timer=window.setTimeout(refresh,5000);}});
    void refresh();return()=>{controller.abort();window.clearTimeout(timer);};
  },[state,offset,reload]);
  async function action(work:()=>Promise<void>) {
    setBusy(true);setError('');setNotice('');try{await work();setReload(v=>v+1);}catch(err){setError(err instanceof Error?err.message:'Не удалось сохранить');}
    finally{setBusy(false);}
  }
  return <section className="key-box term-discovery">
    {onBack&&<button type="button" className="ghost" autoFocus disabled={busy} onClick={onBack}>← К контексту</button>}
    <h2>Предложения терминов</h2>
    {error&&<div className="banner" role="alert">{error}</div>}
    {notice&&<p role="status">{notice}</p>}
    {!data&&!error&&<p role="status">Загружаю предложения…</p>}
    {data&&schedule&&<>
      <details className="term-settings">
      <summary>Настройки и ход анализа · {labels[data.job.status]||data.job.status}</summary>
      <p>Модель протоколов ищет названия и сокращения в новых репликах. Подтверждённые варианты попадают в справочник «Сокращения и названия систем».</p>
      <form className="term-schedule" onSubmit={e=>{e.preventDefault();void action(()=>termDiscoveryAction('settings',schedule));}}>
        <label className="context-toggle"><input type="checkbox" checked={schedule.enabled} disabled={busy}
          onChange={e=>setSchedule({...schedule,enabled:e.target.checked})}/>Искать новые термины ежедневно</label>
        <div className="glossary-fields">
          <label>Время анализа (Москва, UTC+3)<input type="time" value={schedule.run_time} required disabled={busy}
            onChange={e=>setSchedule({...schedule,run_time:e.target.value})}/></label>
          <label>Лимит порций за запуск<input type="number" min={1} max={500} value={schedule.max_batches} required disabled={busy}
            onChange={e=>setSchedule({...schedule,max_batches:Number(e.target.value)})}/></label>
        </div>
        <p className="muted">До 6000 символов в порции. Необработанные реплики остаются на следующий запуск. Выключение расписания не останавливает уже начатый прогон.</p>
        <button className="ghost" disabled={busy}>Сохранить расписание анализа</button>
      </form>
      <div className="term-run" role="status"><strong>{labels[data.job.status]||data.job.status}</strong>
        <span>Порций: {data.job.batches} · Новых вариантов: {data.job.found} · Встреч осталось: {data.job.pending_meetings}</span>
        {data.job.enabled&&<small>Следующий запуск: {new Date(data.job.next_run_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})} МСК</small>}
        {data.job.finished_at&&<small>Последний прогон: {formatDate(data.job.finished_at)}</small>}
        {data.job.error&&<p className="health-warn">{data.job.error}</p>}
      </div>
      <div className="composer-row">
        <button className="ghost" disabled={busy||['queued','running','paused'].includes(data.job.status)}
          onClick={()=>void action(()=>termDiscoveryAction('scan'))}>Проверить новые реплики сейчас</button>
        {['queued','running','paused'].includes(data.job.status)&&<button className="ghost" disabled={busy}
          onClick={()=>void action(()=>termDiscoveryAction('stop'))}>Остановить анализ</button>}
      </div>
      </details>
      <div className="term-tabs" aria-label="Раздел предложений">{states.map(([key,label])=>{
        const count = (data.group_counts ?? data.counts)[key] ?? 0;
        const countLabel = `${data.group_counts ? 'Карточек' : 'Вариантов'} — ${count}`;
        return <button key={key} className={state===key?'primary':'ghost'} title={countLabel} aria-label={`${label}. ${countLabel}`}
          aria-pressed={state===key} disabled={busy} onClick={()=>{if(key!==state||offset>0){setConfirmAll(false);setNotice('');setState(key);setOffset(0);setData(null);}}}>{label} · {count}</button>;
      })}</div>
      {state==='pending'&&data.total>0&&(confirmAll
        ? <div className="bulk-term-review" role="group" aria-label="Подтверждение отклонения">
            <p>Отклонить все карточки на проверке со всех страниц? Сейчас их {data.total.toLocaleString('ru-RU')}. Вернуть отдельные варианты можно в разделе «Отклонённые».</p>
            <div className="composer-row">
              <button type="button" className="ghost" autoFocus disabled={busy} onClick={()=>setConfirmAll(false)}>Отмена</button>
              <button type="button" className="ghost danger" disabled={busy} onClick={()=>void action(async()=>{
                const result=await rejectPendingTerms();
                setConfirmAll(false);setOffset(0);setData(null);
                setNotice(`Отклонено карточек — ${result.groups.toLocaleString('ru-RU')}.`);
              })}>Подтвердить отклонение всех</button>
            </div>
          </div>
        : <button type="button" className="ghost danger" disabled={busy} onClick={()=>setConfirmAll(true)}>Отклонить все</button>)}
      {state==='ordinary'&&<p className="muted">Здесь собраны слова, которые администратор скрыл вместе с их формами. Их можно вернуть в предложения.</p>}
      {!data.proposals.length&&<p>В этом разделе предложений пока нет.</p>}
      {data.proposals.map(p=><ProposalPages key={p.key+state} proposal={p} state={state} busy={busy}
        onReview={payload=>action(()=>termDiscoveryAction('review',payload))}/>) }
      <div className="composer-row"><button className="ghost" disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-20))}>Предыдущие предложения</button>
        <span>{data.total?`${offset+1}–${Math.min(offset+20,data.total)} из ${data.total}`:'0 групп'}</span>
        <button className="ghost" disabled={offset+20>=data.total} onClick={()=>setOffset(v=>v+20)}>Следующие предложения</button></div>
    </>}
  </section>;
}

type ProposalProps = {proposal:TermProposal;state:string;busy:boolean;onReview:(payload:unknown)=>Promise<void>};

function ProposalPages(props:ProposalProps) {
  const {proposal,state,busy} = props;
  const [offset,setOffset] = useState(0);
  const [extra,setExtra] = useState<(Pick<TermProposal,'variants'|'has_more'> & {offset:number})|null>(null);
  const [error,setError] = useState('');
  useEffect(()=>{
    setError('');
    if (!offset) return;
    const controller = new AbortController();
    getTermVariants(state,proposal.key,offset,controller.signal).then(page=>{
      if (controller.signal.aborted) return;
      if (page.variants.length) setExtra({...page,offset});
      else setOffset(0);
    }).catch(err=>{if (!controller.signal.aborted) setError(err.message);});
    return ()=>controller.abort();
  },[proposal,state,offset]);

  const page = offset === 0 ? proposal : (extra?.offset === offset ? extra : null);
  const changePage = (next:number) => {setExtra(null);setError('');setOffset(next);};
  return <>
    {page && <Proposal key={offset} {...props} proposal={{...proposal,...page}}/>}
    {!page && !error && <p role="status">Загружаю варианты…</p>}
    {error && <p role="alert">{error}</p>}
    {(offset > 0 || page?.has_more) && <div className="composer-row" aria-label="Страницы вариантов">
      <button className="ghost" disabled={busy || !offset} onClick={()=>changePage(Math.max(0,offset-20))}>Предыдущие варианты</button>
      {page && <span>Варианты {offset+1}–{offset+page.variants.length}</span>}
      <button className="ghost" disabled={busy || !page?.has_more} onClick={()=>changePage(offset+20)}>Следующие варианты</button>
    </div>}
  </>;
}

function Proposal({proposal,state,busy,onReview}:ProposalProps) {
  const [selected,setSelected]=useState<string[]>([]),[canonical,setCanonical]=useState(proposal.variants[0]?.canonical||'');
  const [description,setDescription]=useState(proposal.variants[0]?.description||''),[department,setDepartment]=useState(proposal.variants[0]?.department||'');
  const [replace,setReplace]=useState(false),[playing,setPlaying]=useState<number|null>(null),[audioError,setAudioError]=useState('');
  const [matches,setMatches]=useState<string[]>([]);
  useEffect(()=>{
    const controller=new AbortController();setMatches([]);
    const timer=window.setTimeout(()=>{if(canonical.trim().length>=2)getGlossaries(canonical,'',0,controller.signal)
      .then(data=>{if(!controller.signal.aborted)setMatches([...new Set(data.terms.map(t=>t.term))].slice(0,10));}).catch(()=>{});},250);
    return()=>{controller.abort();window.clearTimeout(timer);};
  },[canonical]);
  const selectedItems=proposal.variants.filter(v=>selected.includes(v.id)).map(({id,revision})=>({id,revision}));
  function review(action:string){return onReview({items:selectedItems,action,canonical,description,department,replace_text:replace});}
  const hidden = state === 'ordinary';
  function reviewForms(action:'hide_forms'|'restore_forms') {
    const [variant] = proposal.variants;
    if (variant) return onReview({action, items:[{id:variant.id, revision:variant.revision}]});
  }
  return <article className="term-proposal">
    <h3>{proposal.form_label || proposal.variants.map(v=>v.variant).join(' / ')}</h3>
    <p>
      {proposal.form_label && 'Словоформы из реплик · '}
      {!proposal.form_label && proposal.variants.length > 1 && 'Варианты со сходным звучанием · '}
      Встреч с примерами: {proposal.meetings}
    </p>
    {proposal.variants.map(v=><div className="term-variant" key={v.id}>
      {hidden ? <div className="context-toggle"><strong>{v.variant}</strong><small>Встреч: {v.meetings}</small></div>
      : <label className="context-toggle"><input type="checkbox" checked={selected.includes(v.id)} disabled={busy}
        onChange={e=>setSelected(old=>e.target.checked?[...old,v.id]:old.filter(id=>id!==v.id))}/><strong>{v.variant}</strong><small>Встреч: {v.meetings}</small></label>
      }
      {state==='approved'&&<p>→ {v.canonical} · {v.replace_text?'Разрешена замена':'Только поиск контекста'}{v.department?` · ${v.department}`:''}</p>}
      {!v.examples.length&&<p className="muted">Встречи с примерами уже удалены из архива.</p>}
      {v.examples.map(e=><blockquote key={e.id}><p>{e.excerpt}</p><small>{e.title}{e.start!==null?` · ${Math.floor(e.start/60)}:${String(Math.floor(e.start%60)).padStart(2,'0')}`:''}</small>
        {e.audio&&<button className="ghost" onClick={()=>{setPlaying(e.id);setAudioError('');}}>Прослушать реплику</button>}
        {playing===e.id&&<><audio controls autoPlay src={`/api/admin/term-discovery/evidence/${e.id}/audio`} onError={()=>setAudioError('Не удалось воспроизвести фрагмент. Возможно, запись уже удалена.')}/><small>Начало реплики, до 60 секунд.</small></>}
      </blockquote>)}
    </div>)}
    {audioError&&<p role="alert">{audioError}</p>}
    {!hidden&&state!=='approved'&&<>
      <div className="glossary-fields"><label>Правильное написание<input type="text" value={canonical} maxLength={240} list={`terms-${proposal.key}`} disabled={busy}
        placeholder="Введите название или найдите существующий термин" onChange={e=>setCanonical(e.target.value)}/>
        <datalist id={`terms-${proposal.key}`}>{matches.map(term=><option key={term} value={term}/>)}</datalist></label>
        <label>Подразделение из AD (необязательно)<input type="text" value={department} maxLength={120} disabled={busy} onChange={e=>setDepartment(e.target.value)}/></label></div>
      <label>Краткое пояснение<input className="title-input" type="text" value={description} maxLength={4000} disabled={busy} onChange={e=>setDescription(e.target.value)}/></label>
      <label className="context-toggle"><input type="checkbox" checked={replace} disabled={busy} onChange={e=>setReplace(e.target.checked)}/>Автоматически заменять выбранные варианты в новых транскриптах</label>
      <p className="muted">По умолчанию варианты помогают только подбору контекста. Если указано подразделение, замены действуют для встреч его сотрудников по данным AD.</p>
    </>}
    <div className="composer-row">
      {!hidden&&state!=='approved'&&<button className="primary" disabled={busy||!selectedItems.length||!canonical.trim()} onClick={()=>void review('approve')}>Подтвердить выбранные</button>}
      {state==='pending'&&<><button className="ghost" disabled={busy||!selectedItems.length} onClick={()=>void review('defer')}>Отложить на неделю</button>
        <button className="ghost" disabled={busy||!selectedItems.length} onClick={()=>void review('reject')}>Отклонить выбранные</button></>}
      {!hidden&&state!=='pending'&&<button className="ghost" disabled={busy||!selectedItems.length} onClick={()=>void review('reopen')}>
        {state==='approved'?'Отозвать и пересмотреть':'Вернуть на проверку'}</button>}
      {hidden&&<button className="ghost" disabled={busy} onClick={()=>void reviewForms('restore_forms')}>Вернуть слово и его формы</button>}
    </div>
    {proposal.form_label&&(state==='pending'||state==='deferred')&&<div className="term-hide-forms">
      <button className="ghost" disabled={busy} onClick={()=>void reviewForms('hide_forms')}>Обычное слово — скрыть его формы</button>
      <small className="muted">Действует и для будущих предложений. Отменить можно в разделе «Скрытые слова».</small>
    </div>}
  </article>;
}
