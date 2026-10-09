from __future__ import annotations

import logging

from . import config, ldap_auth, store

log = logging.getLogger("mom.people")


def suggest(query: str = "") -> dict:
    """MoM users always. LDAP names only when the directory is on and the query is long enough."""
    needle = query.strip()
    people: list[dict[str, str]] = []
    by_email: dict[str, dict[str, str]] = {}
    for user in store.list_users():
        email = (user.get("email") or "").strip()
        if not email:
            continue
        if needle and needle.casefold() not in email.casefold():
            continue
        item = {"name": email, "email": email, "source": "mom"}
        people.append(item)
        by_email[email.casefold()] = item
    if config.ldap_configured() and len(needle) >= 2:
        try:
            for person in ldap_auth.search_people(needle):
                email = (person.get("email") or "").strip()
                key = email.casefold()
                if key and key in by_email:
                    if person["name"] and person["name"] != by_email[key]["name"]:
                        by_email[key]["name"] = person["name"]
                        by_email[key]["source"] = "ldap"
                    continue
                people.append(
                    {
                        "name": person["name"],
                        "email": email,
                        "source": "ldap",
                    }
                )
                if key:
                    by_email[key] = people[-1]
        except ldap_auth.LdapError:
            log.warning("Каталог не отдал подсказки имён, остаются пользователи MoM")
    return {"people": people[:40], "ldap": config.ldap_configured()}
