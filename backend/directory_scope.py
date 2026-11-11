"""Search-only LDAP allowlist; authentication continues to use its own base/filter."""
from . import config, store

# Only attributes used by speaker assignment or corporate context are eligible.
OPTIONAL_FIELDS = {"job_title": ("title", 200), "department": ("department", 120), "surname": ("sn", 120)}


def speaker_search_mode() -> str:
    saved = store.get_app_setting("ldap_speaker_search") or {}
    return "cache" if saved.get("mode") == "cache" else "live"


def sync_policy() -> dict:
    return store.get_app_setting("ldap_sync_policy") or {"fields": list(OPTIONAL_FIELDS), "domains": []}


def save_sync_policy(fields: list[str], domains: list[str]) -> dict:
    import re

    if len(fields) > len(OPTIONAL_FIELDS) or any(field not in OPTIONAL_FIELDS for field in fields):
        raise ValueError("Выберите поля из списка: должность, подразделение, фамилия")
    if len(domains) > 32:
        raise ValueError("Укажите не больше 32 почтовых доменов")
    normalized = []
    for value in domains:
        try:
            domain = value.strip().removeprefix("@").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Некорректный почтовый домен") from exc
        if len(domain) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain) or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in domain.split(".")
        ):
            raise ValueError("Укажите домен без адреса почты, масок и URL, например company.ru")
        if domain not in normalized:
            normalized.append(domain)
    policy = {"fields": sorted(set(fields)), "domains": sorted(normalized)}
    store.set_app_setting("ldap_sync_policy", policy)
    return policy


def dn_key(value: str) -> tuple:
    from ldap3.utils.dn import parse_dn

    if not value or len(value) > 2000 or any(c in value for c in "\r\n\x00"):
        raise ValueError("Укажите корректный DN области поиска")
    try:
        return tuple((attr.casefold(), part.casefold(), sep) for attr, part, sep in parse_dn(value, strip=True))
    except Exception as exc:
        raise ValueError("Некорректный DN области поиска") from exc


def normalize_bases(values: list[str]) -> list[str]:
    result, seen = [], set()
    for value in values:
        dn = value.strip()
        key = dn_key(dn)
        if key not in seen:
            result.append(dn)
            seen.add(key)
    return result


def snapshot() -> dict:
    saved = store.get_app_setting("ldap_people_scope")
    if saved is None:
        return {"mode": "all", "bases": [], "connection_matches": True}
    return {"mode": saved["mode"], "bases": saved["bases"], "connection_matches":
            saved["url"] == config.get_ldap_url() and saved["base_dn"] == config.get_ldap_base_dn()}


def save(mode: str, bases: list[str]) -> dict:
    if mode not in {"all", "selected"}:
        raise ValueError("Неизвестный режим поиска")
    if len(bases) > 32:
        raise ValueError("Выберите не больше 32 областей")
    store.set_app_setting("ldap_people_scope", {
        "mode": mode, "bases": normalize_bases(bases),
        "url": config.get_ldap_url(), "base_dn": config.get_ldap_base_dn(),
    })
    return snapshot()


def search_bases() -> list[str]:
    settings = snapshot()
    if settings["mode"] == "all":
        return [config.get_ldap_base_dn()]
    if not settings["connection_matches"]:
        # A server/base change must not accidentally broaden a saved allowlist.
        return []
    bases = settings["bases"]
    keys = [dn_key(dn) for dn in bases]
    return [dn for i, dn in enumerate(bases)
            if not any(len(other) < len(keys[i]) and keys[i][-len(other):] == other for other in keys)]
