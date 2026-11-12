from __future__ import annotations

from . import config, corporate_context, directory_scope, ldap_auth, store


def suggest(query: str = "") -> dict:
    """MoM users plus the explicitly selected directory source, with no automatic fallback."""
    needle = query.strip()
    mode = directory_scope.speaker_search_mode()
    people: list[dict[str, str]] = []
    by_email: dict[str, dict[str, str]] = {}
    error = None
    for user in store.search_user_emails(needle):
        email = (user.get("email") or "").strip()
        if not email:
            continue
        if needle and needle.casefold() not in email.casefold():
            continue
        item = {"name": email, "email": email, "source": "mom"}
        people.append(item)
        by_email[email.casefold()] = item
    if config.ldap_configured() and len(needle) >= 2:
        if mode == "cache":
            found, error = corporate_context.search_people(needle)
        else:
            try:
                found = ldap_auth.search_people(needle, for_speakers=True)
            except ldap_auth.LdapError:
                found = []
                error = "Поиск в AD временно недоступен. Можно выбрать сохранённого пользователя или ввести имя вручную."
        for person in found:
            email = (person.get("email") or "").strip()
            key = email.casefold()
            item = {"name": person["name"], "email": email, "source": "ldap"}
            if person.get("job_title"):
                item["job_title"] = person["job_title"]
            if key and key in by_email:
                by_email[key].update(item)
                continue
            people.append(item)
            if key:
                by_email[key] = people[-1]
    folded = needle.casefold()

    def rank(person):
        values = (person["name"].casefold(), person["email"].casefold())
        relevance = 0 if folded in values else 1 if any(v.startswith(folded) for v in values) else 2
        # A directory name is more useful than an email-only account fallback.
        return (person["source"] != "ldap", relevance, *values)

    people.sort(key=rank)
    return {"people": people[:40], "ldap": config.ldap_configured(), "search_mode": mode,
            **({"error": error} if error else {})}


def directory(query: str = "") -> dict:
    """LDAP people with an email, for the admin form that creates a catalog user."""
    needle = query.strip()
    if not config.ldap_configured():
        return {"people": [], "ldap": False}
    if len(needle) < 2:
        return {"people": [], "ldap": True}
    try:
        found = ldap_auth.search_people(needle)
    except ldap_auth.LdapError as exc:
        return {"people": [], "ldap": True, "error": str(exc)}
    people: list[dict[str, str]] = []
    seen: set[str] = set()
    for person in found:
        email = (person.get("email") or "").strip()
        if "@" not in email or " " in email:
            continue
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        people.append({"name": (person.get("name") or email).strip() or email, "email": email})
    return {"people": people, "ldap": True}
