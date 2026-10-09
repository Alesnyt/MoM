from __future__ import annotations

import logging
import ssl
from urllib.parse import urlparse

from . import config

log = logging.getLogger("mom.ldap")


class LdapError(RuntimeError):
    pass


def escape_filter(value: str) -> str:
    escaped = []
    for char in value:
        if char in {"*", "(", ")", "\\", "\x00"}:
            escaped.append("\\" + format(ord(char), "02x"))
        else:
            escaped.append(char)
    return "".join(escaped)


def user_filter(username: str) -> str:
    template = config.get_ldap_user_filter()
    if "{username}" not in template:
        raise LdapError("В фильтре пользователя нет {username}")
    return template.replace("{username}", escape_filter(username))


def people_filter(query: str) -> str:
    escaped = escape_filter(query.strip())
    mail = (config.get_ldap_email_attr() or "mail").strip() or "mail"
    clauses = [f"(cn=*{escaped}*)", f"(displayName=*{escaped}*)", f"({mail}=*{escaped}*)"]
    if mail.lower() != "mail":
        clauses.append(f"(mail=*{escaped}*)")
    return "(|" + "".join(clauses) + ")"


def authenticate(username: str, password: str) -> bool:
    if not password:
        return False
    if not config.ldap_configured():
        raise LdapError("Вход через LDAP выключен")
    try:
        connection, server = _service_connection()
    except LdapError:
        raise
    except Exception as exc:
        log.exception("Нет связи с каталогом")
        raise LdapError("Каталог недоступен") from exc
    try:
        entries = _search(connection, username)
    finally:
        connection.unbind()
    if len(entries) != 1:
        return False
    user_dn, mail = entries[0]
    if "@" in username and mail and mail.lower() != username.strip().lower():
        return False
    return _bind_as(server, user_dn, password)


def test_connection(username: str = "") -> dict:
    if not config.get_ldap_url() or not config.get_ldap_base_dn():
        raise LdapError("Укажите адрес каталога и базу поиска")
    connection, _server = _service_connection()
    try:
        if not username.strip():
            return {"ok": True, "entries": None}
        found = _search(connection, username.strip())
    finally:
        connection.unbind()
    if len(found) == 0:
        raise LdapError("Пользователь в каталоге не найден")
    if len(found) > 1:
        raise LdapError("Фильтр нашёл несколько записей. Сузьте его")
    dn, mail = found[0]
    return {"ok": True, "entries": 1, "dn": dn, "mail": mail or None}


def _service_connection():
    from ldap3 import Connection

    server = _server()
    user = config.get_ldap_bind_dn()
    password = config.get_ldap_bind_password()
    connection = Connection(
        server,
        user=user or None,
        password=password or None,
        auto_bind=False,
        auto_referrals=False,
        receive_timeout=8,
    )
    _open(connection, starttls=config.get_ldap_starttls() and not _is_ldaps())
    if not connection.bind():
        log.warning("Служебная учётная запись каталога не принята: %s", connection.result)
        raise LdapError("Каталог не принял служебную учётную запись")
    return connection, server


def _bind_as(server, user_dn: str, password: str) -> bool:
    from ldap3 import Connection

    connection = Connection(
        server,
        user=user_dn,
        password=password,
        auto_bind=False,
        auto_referrals=False,
        receive_timeout=8,
    )
    try:
        _open(connection, starttls=config.get_ldap_starttls() and not _is_ldaps())
        return bool(connection.bind())
    except LdapError:
        raise
    except Exception as exc:
        log.exception("Проверка пароля в каталоге не удалась")
        raise LdapError("Каталог недоступен") from exc
    finally:
        connection.unbind()


def _search(connection, username: str) -> list[tuple[str, str]]:
    from ldap3 import SUBTREE

    attr = config.get_ldap_email_attr()
    ok = connection.search(
        search_base=config.get_ldap_base_dn(),
        search_filter=user_filter(username),
        search_scope=SUBTREE,
        attributes=[attr] if attr else None,
        size_limit=2,
    )
    if not ok and connection.result and connection.result.get("description") not in {"success", "sizeLimitExceeded"}:
        # No such object / empty result is a failed login, not an outage.
        description = str((connection.result or {}).get("description") or "")
        if description in {"noSuchObject", "sizeLimitExceeded"} or not connection.entries:
            return []
        log.warning("Поиск в каталоге не удался: %s", connection.result)
        raise LdapError("Каталог не выполнил поиск")
    found: list[tuple[str, str]] = []
    for entry in connection.entries:
        mail = ""
        if attr and attr in entry:
            raw = entry[attr].value
            mail = raw if isinstance(raw, str) else (raw[0] if raw else "")
        found.append((entry.entry_dn, str(mail or "")))
    return found


def search_people(query: str, limit: int = 20) -> list[dict[str, str]]:
    text = query.strip()
    if len(text) < 2 or not config.ldap_configured():
        return []
    try:
        connection, _server = _service_connection()
    except LdapError:
        raise
    except Exception as exc:
        log.exception("Нет связи с каталогом")
        raise LdapError("Каталог недоступен") from exc
    try:
        from ldap3 import SUBTREE

        mail_attr = (config.get_ldap_email_attr() or "mail").strip() or "mail"
        attributes = list({mail_attr, "mail", "cn", "displayName"})
        ok = connection.search(
            search_base=config.get_ldap_base_dn(),
            search_filter=people_filter(text),
            search_scope=SUBTREE,
            attributes=attributes,
            size_limit=max(1, min(int(limit), 30)),
        )
        if not ok and not connection.entries:
            description = str((connection.result or {}).get("description") or "")
            if description not in {"success", "sizeLimitExceeded", "noSuchObject"}:
                log.warning("Поиск людей в каталоге не удался: %s", connection.result)
                raise LdapError("Каталог недоступен")
        people: list[dict[str, str]] = []
        for entry in connection.entries:
            mail = _entry_attr(entry, mail_attr) or _entry_attr(entry, "mail")
            name = _entry_attr(entry, "displayName") or _entry_attr(entry, "cn") or mail
            if not name:
                continue
            people.append({"name": name, "email": mail, "source": "ldap"})
        return people
    finally:
        connection.unbind()


def _entry_attr(entry: object, name: str) -> str:
    try:
        if name not in entry:
            return ""
        raw = entry[name].value
    except Exception:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)) and raw:
        return str(raw[0]).strip()
    return str(raw or "").strip()


def _server():
    from ldap3 import Server, Tls

    host, port, use_ssl = _target()
    tls = Tls(validate=ssl.CERT_REQUIRED if config.get_ldap_tls_verify() else ssl.CERT_NONE)
    return Server(host, port=port, use_ssl=use_ssl, tls=tls, connect_timeout=8, get_info=None)


def _open(connection, *, starttls: bool) -> None:
    if not connection.open():
        raise LdapError("Каталог недоступен")
    if starttls and not connection.start_tls():
        raise LdapError("Не удалось включить STARTTLS")


def _is_ldaps() -> bool:
    return config.get_ldap_url().lower().startswith("ldaps://")


def _target() -> tuple[str, int, bool]:
    parsed = urlparse(config.get_ldap_url())
    if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
        raise LdapError("Адрес каталога должен начинаться с ldap:// или ldaps://")
    use_ssl = parsed.scheme == "ldaps"
    port = parsed.port or (636 if use_ssl else 389)
    return parsed.hostname, port, use_ssl
