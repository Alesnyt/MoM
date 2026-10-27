from __future__ import annotations

import base64
import logging
import json
import os
import re
import time
import ssl
from urllib.parse import urlparse

from . import config, directory_scope

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
    if not re.fullmatch(r"(?:[A-Za-z][A-Za-z0-9-]*|[0-9]+(?:\.[0-9]+)+)", mail):
        raise LdapError("Некорректное имя атрибута email")
    clauses = [
        f"(cn=*{escaped}*)",
        f"(displayName=*{escaped}*)",
        f"(givenName=*{escaped}*)",
        f"(sn=*{escaped}*)",
        f"({mail}=*{escaped}*)",
    ]
    if mail.lower() != "mail":
        clauses.append(f"(mail=*{escaped}*)")
    return "(&(objectClass=person)(!(objectClass=computer))(|" + "".join(clauses) + "))"


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


def authentication_bases() -> list[str]:
    """Optional full DN scopes for login, independent from speaker suggestions."""
    raw = os.getenv("LDAP_AUTH_BASE_DNS", "").strip()
    if not raw:
        if os.getenv("LDAP_ALLOWED_OU", "").strip():
            raise LdapError("Замените LDAP_ALLOWED_OU на LDAP_AUTH_BASE_DNS с полными DN")
        return [config.get_ldap_base_dn()]
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not 1 <= len(values) <= 16 or not all(isinstance(v, str) for v in values):
            raise ValueError()
        bases = directory_scope.normalize_bases(values)
        root = directory_scope.dn_key(config.get_ldap_base_dn())
        keys = [directory_scope.dn_key(dn) for dn in bases]
        if any(len(key) < len(root) or key[-len(root):] != root for key in keys):
            raise ValueError()
        # Avoid duplicate results from overlapping subtrees.
        return [dn for i, dn in enumerate(bases)
                if not any(len(other) < len(keys[i]) and keys[i][-len(other):] == other for other in keys)]
    except (ValueError, TypeError):
        raise LdapError("LDAP_AUTH_BASE_DNS: нужен непустой JSON-массив полных DN внутри LDAP_BASE_DN") from None


def _search(connection, username: str) -> list[tuple[str, str]]:
    from ldap3 import SUBTREE, DEREF_NEVER

    attr = config.get_ldap_email_attr()
    found: list[tuple[str, str]] = []
    seen = set()
    deadline = time.monotonic() + 12
    for base in authentication_bases():
        if time.monotonic() >= deadline:
            raise LdapError("Поиск учётной записи занял слишком много времени")
        ok = connection.search(
            search_base=base, search_filter=user_filter(username),
            search_scope=SUBTREE, dereference_aliases=DEREF_NEVER,
            attributes=[attr] if attr else None, size_limit=2, time_limit=5,
        )
        description = str((connection.result or {}).get("description") or "")
        # Never authenticate from a truncated or failed directory response.
        if description == "sizeLimitExceeded":
            raise LdapError("Фильтр нашёл несколько записей. Сузьте области входа или фильтр")
        if description not in {"", "success"} or (not ok and connection.entries):
            raise LdapError("Не удалось выполнить поиск в области входа. Проверьте DN и права")
        for entry in connection.entries:
            key = directory_scope.dn_key(str(entry.entry_dn))
            base_key = directory_scope.dn_key(base)
            if len(key) < len(base_key) or key[-len(base_key):] != base_key:
                raise LdapError("Каталог вернул запись вне выбранной области входа")
            if key in seen:
                continue
            seen.add(key)
            mail = ""
            if attr and attr in entry:
                raw = entry[attr].value
                mail = raw if isinstance(raw, str) else (raw[0] if raw else "")
            found.append((str(entry.entry_dn), str(mail or "")))
            if len(found) > 1:
                return found
    return found


def search_people(query: str, limit: int = 20) -> list[dict[str, str]]:
    text = query.strip()
    if len(text) < 2 or not config.ldap_configured():
        return []
    bases = directory_scope.search_bases()
    if not bases:
        return []
    connection = None
    try:
        connection, _server = _service_connection()
        from ldap3 import SUBTREE, DEREF_NEVER

        mail_attr = (config.get_ldap_email_attr() or "mail").strip() or "mail"
        attributes = sorted({mail_attr, "mail", "cn", "displayName"})
        people: list[dict[str, str]] = []
        seen: set[str] = set()
        cap = max(1, min(int(limit), 30))
        deadline = time.monotonic() + 12
        for base in bases:
            if time.monotonic() >= deadline:
                raise LdapError("Поиск по выбранным областям занял слишком много времени. Сократите список областей")
            ok = connection.search(
                search_base=base, search_filter=people_filter(text), search_scope=SUBTREE,
                dereference_aliases=DEREF_NEVER, attributes=attributes,
                size_limit=cap, time_limit=5,
            )
            description = str((connection.result or {}).get("description") or "")
            if not ok and description not in {"success", "sizeLimitExceeded"}:
                raise LdapError("Не удалось выполнить поиск в выбранной области. Проверьте DN и права служебной учётной записи")
            for entry in connection.entries:
                mail = _entry_attr(entry, mail_attr) or _entry_attr(entry, "mail")
                name = _entry_attr(entry, "displayName") or _entry_attr(entry, "cn") or mail
                key = mail.casefold() if mail else str(entry.entry_dn).casefold()
                if not name or key in seen:
                    continue
                seen.add(key)
                people.append({"name": name, "email": mail, "source": "ldap"})
                if len(people) >= cap:
                    return people
        return people
    except LdapError:
        raise
    except Exception as exc:
        log.exception("Поиск людей в каталоге не удался")
        raise LdapError("Каталог недоступен") from exc
    finally:
        if connection is not None:
            connection.unbind()


def browse_scopes(base_dn: str = "", cursor: str = "") -> dict:
    """Read directory structure, never enumerate people or follow referrals."""
    from ldap3 import BASE, LEVEL, DEREF_NEVER

    if not config.get_ldap_url() or not config.get_ldap_base_dn():
        raise LdapError("Сначала сохраните подключение к каталогу и базу поиска")
    connection = None
    try:
        if base_dn:
            directory_scope.dn_key(base_dn)
        cookie = base64.b64decode(cursor, validate=True) if cursor else None
        connection, _server = _service_connection()
        if not base_dn:
            roots = [{"dn": config.get_ldap_base_dn(), "name": "Настроенная база поиска", "kind": "base"}]
            ok = connection.search(search_base="", search_filter="(objectClass=*)", search_scope=BASE,
                attributes=["namingContexts", "defaultNamingContext"], time_limit=5)
            contexts = []
            if ok and connection.entries:
                for attr in ("defaultNamingContext", "namingContexts"):
                    if attr in connection.entries[0]:
                        value = connection.entries[0][attr].value
                        contexts.extend([value] if isinstance(value, str) else value or [])
            seen = {directory_scope.dn_key(roots[0]["dn"])}
            # A naming context can also be Schema, Configuration or DNS zones.
            # Ask for domain objects explicitly before offering it as a domain.
            deadline = time.monotonic() + 12
            for dn in list(dict.fromkeys(contexts))[:32]:
                if time.monotonic() >= deadline:
                    break
                key = directory_scope.dn_key(dn)
                if key in seen:
                    continue
                seen.add(key)
                found = connection.search(search_base=dn, search_filter="(|(objectClass=domainDNS)(objectClass=domain))",
                    search_scope=BASE, attributes=["name", "dc"], time_limit=3)
                if found and connection.entries:
                    roots.append({"dn": dn, "name": _entry_attr(connection.entries[0], "name") or dn, "kind": "domain"})
            return {"items": roots, "next_cursor": None,
                    "warning": "Показаны области, доступные через настроенный сервер. Для другого домена может потребоваться отдельное подключение."}
        ok = connection.search(search_base=base_dn,
            search_filter="(|(objectClass=organizationalUnit)(objectClass=container)(objectClass=domainDNS))",
            search_scope=LEVEL, dereference_aliases=DEREF_NEVER, attributes=["name", "ou", "cn"],
            paged_size=100, paged_cookie=cookie, size_limit=2000, time_limit=5)
        result = connection.result or {}
        description = str(result.get("description") or "")
        if not ok and description not in {"success", "sizeLimitExceeded"}:
            raise LdapError("Не удалось прочитать область каталога. Проверьте DN и права служебной учётной записи")
        items = [{"dn": entry.entry_dn, "name": _entry_attr(entry, "name") or _entry_attr(entry, "ou")
                  or _entry_attr(entry, "cn") or entry.entry_dn, "kind": "container"} for entry in connection.entries]
        cookie = result.get("controls", {}).get("1.2.840.113556.1.4.319", {}).get("value", {}).get("cookie")
        return {"items": items, "next_cursor": base64.b64encode(cookie).decode() if cookie else None,
                "warning": "Каталог ограничил список. Можно указать нужный DN вручную." if description == "sizeLimitExceeded" else None}
    except LdapError:
        raise
    except (ValueError, TypeError) as exc:
        raise LdapError("Некорректный DN области или указатель страницы") from exc
    except Exception as exc:
        log.exception("Не удалось прочитать структуру каталога")
        raise LdapError("Каталог недоступен") from exc
    finally:
        if connection is not None:
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
    # ldap3 Connection.open() returns None, check the socket state instead.
    connection.open()
    if connection.closed:
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
