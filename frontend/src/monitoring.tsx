import { useEffect, useRef, useState } from "react";
import { downloadDiagnostics, getDiagnostics, getMonitoring } from "./api";
import { formatBytes, formatClock, formatDate } from "./format";
import { stepLabel } from "./meeting";
import { startPolling } from "./poll";
import type { DiagnosticsPeriod, DiagnosticsSummary, MonitoringSnapshot } from "./types";

const REFRESH_MS = 5000;
const number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const stageLabels = {
  queue_wait: "Ожидание в очереди", audio_prepare: "Подготовка аудио", asr: "Распознавание речи",
  asr_cloud: "Облачное распознавание", diarization: "Разделение спикеров",
  name_review: "Проверка имён", protocol: "Сборка протокола",
};
const diagnosticStageLabels: Record<string, string> = {
  ...stageLabels,
  local_slot_wait: "Ожидание распознавания",
  gigaam_model_load: "Загрузка GigaAM",
  whisper_model_load: "Загрузка Whisper",
  diarization_model_load: "Загрузка модели спикеров",
  diarization_audio_load: "Чтение аудио для спикеров",
  diarization_inference: "Вычисление спикеров",
  upload_receive: "Получение записи",
  upload_save: "Сохранение записи",
  context_load: "Загрузка справочного контекста",
  context_apply: "Применение справочного контекста",
  result_save: "Сохранение результата",
};
const runPhaseLabels = { audio: "Расшифровка", protocol: "Протокол", upload: "Загрузка" };
const runStatusLabels: Record<string, string> = {
  running: "В работе", done: "Готово", review: "Готово к разметке", uploaded: "Загружено",
  error: "Ошибка", interrupted: "Прервано", stopped: "Остановлено", cancelled: "Отменено",
};

function Metric({ label, value, detail }: { label: string; value: string | number; detail: string }) {
  return (
    <div className="monitor-metric">
      <dt>{label}</dt>
      <dd>{value}<small>{detail}</small></dd>
    </div>
  );
}

export function MonitoringPanel() {
  const [snapshot, setSnapshot] = useState<MonitoringSnapshot | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => startPolling(async (signal) => {
    try {
      return await getMonitoring(signal);
    } catch (error) {
      if (!signal.aborted) setFailed(true);
      throw error;
    }
  }, (next) => {
    setSnapshot(next);
    setFailed(false);
  }, REFRESH_MS, { immediate: true }), []);

  return (
    <div className="monitor">
      <div className="monitor-update">
        <span>Автообновление каждые {REFRESH_MS / 1000} с</span>
        {snapshot && (
          <span>Обновлено <time dateTime={snapshot.sampled_at}>{new Date(snapshot.sampled_at).toLocaleTimeString("ru-RU")}</time></span>
        )}
      </div>
      {failed && (
        <div className="banner" role="status">
          Не удалось обновить мониторинг. {snapshot && "Показаны данные последнего обновления. "}Повторю автоматически.
        </div>
      )}
      {snapshot ? <MonitoringStats snapshot={snapshot} /> : !failed && <p role="status">Загружаю показатели…</p>}
      <DiagnosticsPanel />
    </div>
  );
}

function DiagnosticsPanel() {
  const [days, setDays] = useState<DiagnosticsPeriod>(7);
  const [summary, setSummary] = useState<DiagnosticsSummary | null>(null);
  const [failed, setFailed] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const download = useRef<AbortController | null>(null);

  useEffect(() => () => download.current?.abort(), []);
  useEffect(() => startPolling(async (signal) => {
    try {
      return await getDiagnostics(days, signal);
    } catch (error) {
      if (!signal.aborted) setFailed(true);
      throw error;
    }
  }, (next) => {
    setSummary(next);
    setFailed(!next.available);
  }, 15000, { immediate: true }), [days, refresh]);

  async function exportReport() {
    const controller = new AbortController();
    download.current?.abort();
    download.current = controller;
    setExporting(true);
    setExportError(null);
    try {
      await downloadDiagnostics(days, controller.signal);
    } catch {
      if (!controller.signal.aborted) setExportError("Не удалось скачать отчёт. Повторите попытку.");
    } finally {
      if (!controller.signal.aborted) setExporting(false);
    }
  }

  return (
    <section className="monitor-diagnostics" aria-labelledby="monitor-diagnostics-title">
      <h2 id="monitor-diagnostics-title">Диагностика производительности</h2>
      <p className="progress-hint">Замеры новых обработок сохраняются после перезапуска. История — до 14 дней, не более 1000 запусков.
        Распознавание и сборка протокола считаются отдельными запусками; ожидание ручной разметки в их время не входит.</p>
      <div className="actions-row">
        <label className="audit-size">Период отчёта
          <select value={days} disabled={exporting} onChange={(event) => {
            setSummary(null);
            setFailed(false);
            setExportError(null);
            setDays(Number(event.target.value) as DiagnosticsPeriod);
          }}>
            <option value={1}>Последние сутки</option>
            <option value={7}>7 дней</option>
            <option value={14}>14 дней</option>
          </select>
        </label>
        <button className="ghost" type="button" disabled={exporting} onClick={() => {
          setFailed(false);
          setRefresh((value) => value + 1);
        }}>Обновить диагностику</button>
        <button type="button" disabled={exporting || !summary?.available || failed} onClick={() => void exportReport()}>
          {exporting ? "Готовлю отчёт…" : "Скачать отчёт"}
        </button>
      </div>
      <p className="progress-hint">Файл JSON содержит время этапов, показатели CPU и памяти, параметры обработки и счётчики запросов LLM.
        Тексты встреч, имена, адреса серверов и ключи не включаются. Ничего не отправляется автоматически.</p>
      {failed && <div className="banner" role="status">Диагностика временно недоступна. {summary?.available && "Показаны последние полученные замеры. "}
        Обработка встреч продолжается. Повторю загрузку автоматически.</div>}
      {exportError && <div className="banner" role="alert">{exportError}</div>}
      {!summary && !failed && <p role="status">Загружаю диагностику…</p>}
      {summary?.available && <>
        <dl className="monitor-metrics">
          <Metric label="Сохранено запусков" value={summary.runs} detail="За выбранный период" />
          <Metric label="Завершено успешно" value={summary.completed} detail="Расшифровки, протоколы и загрузки" />
          <Metric label="С ошибкой" value={summary.errors} detail="Не завершились успешно" />
          <Metric label="Прервано" value={summary.interrupted} detail={`Сейчас в работе: ${summary.active}`} />
        </dl>
        {summary.runs === 0 && <p className="monitor-empty">Замеров пока нет. Обработайте несколько встреч и скачайте отчёт.
          Данные прежних обработок не восстанавливаются.</p>}
        {Object.keys(summary.stages).length > 0 && <details className="diagnostics-details">
          <summary>Время по этапам за выбранный период</summary>
          <p className="progress-hint">Этапы могут быть вложенными: например, загрузка модели уже включена во время распознавания.
            Складывать все строки нельзя.</p>
          <div className="audit-table-scroll" tabIndex={0} role="region" aria-label="Время этапов диагностики">
            <table className="monitor-jobs" aria-label="Сохранённые замеры этапов">
              <thead><tr><th scope="col">Этап</th><th scope="col">Медиана</th><th scope="col">95% укладываются в</th><th scope="col">Замеров</th></tr></thead>
              <tbody>{Object.entries(summary.stages).map(([stage, value]) => <tr key={stage}>
                <th scope="row">{diagnosticStageLabels[stage] ?? "Другой этап"}</th>
                <td>{number.format(value.median_seconds)} с</td><td>{number.format(value.p95_seconds)} с</td><td>{value.samples}</td>
              </tr>)}</tbody>
            </table>
          </div>
        </details>}
        {summary.recent_runs.length > 0 && <details className="diagnostics-details">
          <summary>Последние запуски</summary>
          <p className="progress-hint">Пик памяти учитывает MoM и дочерние процессы. Память основного процесса после простоя появляется у последней обработки аудио, когда очередь выгрузит модели.</p>
          <div className="audit-table-scroll" tabIndex={0} role="region" aria-label="Последние запуски диагностики">
            <table className="monitor-jobs" aria-label="История обработок">
              <thead><tr><th scope="col">Начало</th><th scope="col">Встреча</th><th scope="col">Обработка</th><th scope="col">Результат</th>
                <th scope="col">Время</th><th scope="col">Фрагменты GigaAM</th><th scope="col">Запросов LLM</th>
                <th scope="col">Повторов LLM</th><th scope="col">Пик памяти MoM</th><th scope="col">После выгрузки</th></tr></thead>
              <tbody>{summary.recent_runs.map((run) => <tr key={run.run}>
                <td><time dateTime={run.started_at}>{formatDate(run.started_at)}</time></td>
                <td>{run.meeting}</td><td>{runPhaseLabels[run.phase] ?? "Обработка"}</td><td>{runStatusLabels[run.status] ?? "Неизвестно"}</td>
                <td>{run.elapsed_seconds === null ? "—" : formatClock(run.elapsed_seconds)}</td>
                <td>{run.gigaam_fragments == null ? "—" : number.format(run.gigaam_fragments)}
                  {run.gigaam_source_seconds != null && ` · ${formatClock(run.gigaam_source_seconds)}`}</td>
                <td>{run.llm_calls}</td><td>{run.sdk_retries ?? "—"}</td>
                <td>{run.peak_rss_bytes == null ? "—" : formatBytes(run.peak_rss_bytes)}</td>
                <td>{run.after_idle_rss_bytes == null ? "—" : formatBytes(run.after_idle_rss_bytes)}</td>
              </tr>)}</tbody>
            </table>
          </div>
        </details>}
      </>}
    </section>
  );
}

function MonitoringStats({ snapshot: { queue, resources, disk, performance } }: { snapshot: MonitoringSnapshot }) {
  const cpuDetail = resources.rss_bytes === null ? "Показатель недоступен"
    : resources.cpu_percent === null ? "Измеряю нагрузку…" : "100% соответствует одному ядру";
  return (
    <>
      <section aria-labelledby="monitor-queue">
        <h2 id="monitor-queue">Обработка встреч</h2>
        <dl className="monitor-metrics">
          <Metric label="В работе" value={`${queue.active} из ${queue.limit}`} detail="Занято слотов обработки" />
          <Metric label="В очереди" value={queue.waiting} detail={queue.oldest_wait_seconds == null
            ? "Ждут запуска" : `Самая долгая заявка · ${formatClock(queue.oldest_wait_seconds)}`} />
          <Metric label="На разметке" value={queue.review} detail="Ждут подтверждения спикеров" />
        </dl>
      </section>
      {performance && <section aria-labelledby="monitor-performance">
        <h2 id="monitor-performance">Скорость и ошибки</h2>
        <p className="monitor-empty">Ошибки за последние 24 часа — {performance.errors} из {performance.finished}
          {performance.finished ? ` (${number.format(100 * performance.errors / performance.finished)}%)` : ""}. После перезапуска статистика собирается заново.</p>
        <table className="monitor-jobs" aria-labelledby="monitor-performance">
          <thead><tr><th scope="col">Этап</th><th scope="col">Медиана</th><th scope="col">95% укладываются в</th><th scope="col">Замеров</th></tr></thead>
          <tbody>{Object.entries(stageLabels).map(([name, label]) => {
            const stage = performance.stages[name];
            return <tr key={name}><th scope="row">{label}</th>
              <td>{stage ? `${number.format(stage.median_seconds)} с` : "—"}</td>
              <td>{stage ? `${number.format(stage.p95_seconds)} с` : "—"}</td>
              <td>{stage?.samples ?? 0}</td></tr>;
          })}</tbody>
        </table>
      </section>}
      <section aria-labelledby="monitor-resources">
        <h2 id="monitor-resources">Ресурсы</h2>
        <dl className="monitor-metrics">
          <Metric label="CPU MoM"
            value={resources.cpu_percent === null ? "—" : `${number.format(resources.cpu_percent)}%`}
            detail={cpuDetail} />
          <Metric label="Память MoM"
            value={resources.rss_bytes === null ? "—" : formatBytes(resources.rss_bytes)}
            detail={resources.process_count === null ? "Показатель недоступен" : `Процессов MoM: ${resources.process_count} · RSS`} />
          <Metric label="Свободно на диске" value={formatBytes(disk.free_bytes)}
            detail={`Из ${formatBytes(disk.total_bytes)} · записи занимают ${formatBytes(disk.recordings_bytes)}`} />
        </dl>
        {!disk.ok && <p className="health-warn">Свободного места меньше резерва ({formatBytes(disk.min_free_bytes)}). Новые записи не принимаются.</p>}
      </section>
      <section aria-labelledby="monitor-active">
        <h2 id="monitor-active">Активные задачи</h2>
        {queue.jobs.length ? (
          <table className="monitor-jobs" aria-labelledby="monitor-active">
            <thead><tr><th scope="col">Задача</th><th scope="col">Этап</th><th scope="col">Прогресс</th></tr></thead>
            <tbody>
              {queue.jobs.map((job) => {
                const id = job.id.slice(0, 8);
                const progress = Math.max(0, Math.min(100, job.progress));
                return (
                  <tr key={job.id}>
                    <th scope="row"><code title={job.id}>{id}</code></th>
                    <td>{stepLabel(job.status)}</td>
                    <td><div className="monitor-progress">
                      <progress value={progress} max={100} aria-label={`Прогресс задачи ${id}`} />
                      <span>{progress}%</span>
                    </div></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="monitor-empty">Сейчас нет активных задач.</p>}
      </section>
    </>
  );
}
