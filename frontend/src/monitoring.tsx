import { useEffect, useState } from "react";
import { getMonitoring } from "./api";
import { formatBytes } from "./format";
import { stepLabel } from "./meeting";
import { startPolling } from "./poll";
import type { MonitoringSnapshot } from "./types";

const REFRESH_MS = 5000;
const number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });

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
    </div>
  );
}

function MonitoringStats({ snapshot: { queue, resources, disk } }: { snapshot: MonitoringSnapshot }) {
  const cpuDetail = resources.rss_bytes === null ? "Показатель недоступен"
    : resources.cpu_percent === null ? "Измеряю нагрузку…" : "100% соответствует одному ядру";
  return (
    <>
      <section aria-labelledby="monitor-queue">
        <h2 id="monitor-queue">Обработка встреч</h2>
        <dl className="monitor-metrics">
          <Metric label="В работе" value={`${queue.active} из ${queue.limit}`} detail="Занято слотов обработки" />
          <Metric label="В очереди" value={queue.waiting} detail="Ждут запуска" />
          <Metric label="На разметке" value={queue.review} detail="Ждут подтверждения спикеров" />
        </dl>
      </section>
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
