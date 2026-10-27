from __future__ import annotations

from . import store
from .sheet import csv_bytes, xlsx_bytes

PAGE_SIZES = (20, 40, 100)

LABELS = {
    "user.create": "Создан пользователь",
    "user.delete": "Удалён пользователь",
    "user.auth": "Сменён способ входа",
    "user.password": "Сброшен пароль",
    "user.limit": "Изменён архив",
    "ldap.scopes": "Изменены области поиска в каталоге",
    "ldap.save": "Изменён каталог",
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
}

HEADERS = ["Время", "Кто", "Событие", "Код", "Объект", "Подробности"]


def present(row: dict) -> dict:
    item = dict(row)
    action = str(item.get("action") or "")
    item["label"] = LABELS.get(action, action)
    return item


def page(limit: int) -> list[dict]:
    return [present(row) for row in store.list_audit(limit)]


def export_table() -> tuple[list[str], list[list[str]]]:
    rows = []
    for row in store.list_audit(store.AUDIT_KEEP):
        action = str(row.get("action") or "")
        rows.append(
            [
                str(row.get("at") or ""),
                str(row.get("actor") or ""),
                LABELS.get(action, action),
                action,
                str(row.get("target") or ""),
                str(row.get("detail") or ""),
            ]
        )
    return HEADERS, rows


def export_csv() -> bytes:
    headers, rows = export_table()
    return csv_bytes(headers, rows)


def export_xlsx() -> bytes:
    headers, rows = export_table()
    return xlsx_bytes(headers, rows)
