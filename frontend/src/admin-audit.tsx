import { useEffect, useState } from "react";
import { listAudit, downloadAudit } from "./api";
import { formatDate } from "./format";
import type { AuditEvent } from "./types";

const AUDIT_LABELS: Record<string, string> = {
  "user.create": "Создан пользователь",
  "user.delete": "Удалён пользователь",
  "user.auth": "Сменён способ входа",
  "user.password": "Сброшен пароль",
  "user.limit": "Изменён архив",
  "ldap.scopes": "Изменены области поиска в каталоге",
  "ldap.save": "Изменён каталог",
  "ldap.sync.request": "Запрошено обновление кэша AD",
  "ldap.sync.error": "Ошибка обновления кэша AD",
  "ldap.schedule": "Изменено расписание обновления AD",
  "meeting.delete": "Удалена встреча",
  "meeting.stop": "Обработка остановлена",
  "meeting.restart": "Обработка запущена заново",
  "prompts.save": "Сохранены промпты LLM",
  "speaker.rename": "Переименован спикер",
  "speaker.reassign": "Реплики перенесены",
  "email.send": "Протокол отправлен",
  "email.open": "Открыт черновик письма",
  "email.fail": "Письмо не ушло",
  "admin.setup": "Создан администратор",
  "admin.login": "Вход администратора",
  "admin.login.fail": "Неудачный вход администратора",
  "user.login": "Вход пользователя",
  "user.login.fail": "Неудачный вход",
  "key.save": "Сохранён ключ",
  "key.delete": "Ключ удалён",
  "models.save": "Сменены модели",
  "smtp.save": "Изменена почта",
};

const AUDIT_PAGE_SIZES = [20, 40, 100] as const;

export function AuditPanel() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [limit, setLimit] = useState<(typeof AUDIT_PAGE_SIZES)[number]>(20);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<"csv" | "xlsx" | null>(null);

  function load(size = limit) {
    setError(null);
    listAudit(size)
      .then(setEvents)
      .catch((err: Error) => {
        setEvents([]);
        setError(err.message);
      });
  }

  useEffect(() => {
    load(limit);
  }, [limit]);

  async function onExport(format: "csv" | "xlsx") {
    setError(null);
    setExporting(format);
    try {
      await downloadAudit(format);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось выгрузить журнал");
    } finally {
      setExporting(null);
    }
  }

  return (
    <div className="audit-panel">
      <div className="actions-row">
        <label className="audit-size">
          Показать
          <select
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value) as (typeof AUDIT_PAGE_SIZES)[number])}
          >
            {AUDIT_PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
        <button className="ghost" type="button" onClick={() => load()} disabled={exporting !== null}>
          Обновить
        </button>
        <button className="ghost" type="button" onClick={() => void onExport("csv")} disabled={exporting !== null}>
          {exporting === "csv" ? "Готовлю CSV…" : "CSV"}
        </button>
        <button className="ghost" type="button" onClick={() => void onExport("xlsx")} disabled={exporting !== null}>
          {exporting === "xlsx" ? "Готовлю XLSX…" : "XLSX"}
        </button>
      </div>
      <p className="progress-hint">На экране последние {limit}. Файлы содержат весь журнал.</p>
      {error && <div className="banner" role="alert">{error}</div>}
      {events === null ? (
        <p>Загружаю журнал…</p>
      ) : events.length === 0 ? (
        <p>Пока пусто. Здесь появятся создание пользователей, смена каталога, удаление встреч, правка спикеров и отправка почты.</p>
      ) : (
        <div className="audit-table-scroll" tabIndex={0} role="region" aria-label="Журнал действий">
          <table className="audit-table">
            <thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Объект</th><th>Подробности</th></tr></thead>
            <tbody>{events.map(event => <tr key={event.id}>
              <td><time dateTime={event.at}>{formatDate(event.at)}</time></td>
              <td>{event.actor}</td>
              <td>{event.label || AUDIT_LABELS[event.action] || event.action}</td>
              <td>{event.target || "—"}</td>
              <td>{event.detail || "—"}</td>
            </tr>)}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}
