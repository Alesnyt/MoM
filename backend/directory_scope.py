"""Search-only LDAP allowlist; authentication continues to use its own base/filter."""
import json
import re

from . import config, store

# Only attributes used by speaker assignment or corporate context are eligible.
OPTIONAL_FIELDS = {"job_title": ("title", 200), "department": ("department", 120), "surname": ("sn", 120)}
DEFAULT_LDAP_FILTER = "(!(userAccountControl:1.2.840.113556.1.4.803:=2))"
_LDAP_IDENTIFIER = r"(?:[A-Za-z][A-Za-z0-9-]*|[0-9]+(?:\.[0-9]+)+)"
_LDAP_ATTRIBUTE = re.compile(rf"{_LDAP_IDENTIFIER}(?:;[A-Za-z0-9-]+)*")


def speaker_search_mode() -> str:
    saved = store.get_app_setting("ldap_speaker_search") or {}
    return "cache" if saved.get("mode") == "cache" else "live"


def sync_policy() -> dict:
    return {"fields": list(OPTIONAL_FIELDS), "domains": [], "ldap_filter": DEFAULT_LDAP_FILTER,
            **(store.get_app_setting("ldap_sync_policy") or {})}


def _validate_ldap_filter(value: str) -> str:
    from ldap3.core.exceptions import LDAPInvalidFilterError
    from ldap3.operation.search import NOT, compile_filter, parse_filter
    from pyasn1.codec.ber.encoder import encode
    from pyasn1.error import PyAsn1Error

    if len(value) > 4096:
        raise ValueError("LDAP-фильтр должен содержать не больше 4096 символов")
    value = value.strip()
    if not value:
        return ""

    error = "Некорректный LDAP-фильтр. Укажите одно выражение в скобках, например (department=IT)"
    if re.search(r"[\x00\ud800-\udfff]|\\(?![0-9a-fA-F]{2})|\(\s*\)", value):
        raise ValueError(error)
    try:
        root = parse_filter(value, schema=None, auto_escape=False, auto_encode=True, validator=None, check_names=False)
    except LDAPInvalidFilterError as exc:
        raise ValueError(error) from exc

    nodes = [(root, 0)]
    while nodes:
        node, depth = nodes.pop()
        if depth > 64:
            raise ValueError("LDAP-фильтр слишком глубоко вложен. Упростите выражение")
        if node.assertion is None:
            if not node.elements or (node.tag == NOT and len(node.elements) != 1):
                raise ValueError(error)
            nodes.extend((child, depth + 1) for child in node.elements)
        else:
            attribute = node.assertion["attr"]
            rule = node.assertion.get("matchingRule")
            if ((attribute and not _LDAP_ATTRIBUTE.fullmatch(attribute))
                    or (rule and not re.fullmatch(_LDAP_IDENTIFIER, rule))
                    or not (attribute or rule)):
                raise ValueError(error)
    try:
        encode(compile_filter(root.elements[0]))
    except PyAsn1Error as exc:
        raise ValueError(error) from exc
    return value


def save_sync_policy(fields: list[str], domains: list[str], ldap_filter: str | None = None) -> dict:
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
    if ldap_filter is not None:
        policy["ldap_filter"] = _validate_ldap_filter(ldap_filter)
    # An already open admin tab can save fields/domains without sending a filter.
    with store.connect() as conn:
        row = conn.execute("INSERT INTO app_settings (key, payload) VALUES ('ldap_sync_policy', ?) "
                           "ON CONFLICT(key) DO UPDATE SET payload=json_patch(app_settings.payload, excluded.payload) "
                           "RETURNING payload", (json.dumps(policy, ensure_ascii=False),)).fetchone()
    return {"ldap_filter": DEFAULT_LDAP_FILTER, **json.loads(row["payload"])}


def _preserve_escaped_spaces(value: str) -> str:
    # ldap3's strip=True removes escaped trailing spaces too. Hex escapes survive
    # trimming, including when a DN is pasted with padding around its components.
    return re.sub(r"\\(?:[0-9a-fA-F]{2}|.)", lambda match: r"\20" if match[0] == "\\ " else match[0], value)


def _dn_value(value: str) -> str:
    raw = bytearray()
    index = 0
    while index < len(value):
        if value[index] == "\\":
            index += 1
            pair = value[index:index + 2]
            if len(pair) == 2 and all(char in "0123456789abcdefABCDEF" for char in pair):
                raw.append(int(pair, 16))
                index += 2
                continue
        raw.extend(value[index].encode("utf-8"))
        index += 1
    return raw.decode("utf-8").casefold()


def dn_key(value: str) -> tuple:
    from ldap3.utils.dn import parse_dn

    if not value or len(value) > 2000 or any(c in value for c in "\r\n\x00"):
        raise ValueError("Укажите корректный DN области поиска")
    try:
        # RDN order defines the hierarchy; attributes inside a multi-valued RDN
        # are unordered. Decode escapes only after parsing so an escaped comma
        # can never turn into a subtree boundary.
        rdns, attributes = [], []
        for attr, part, sep in parse_dn(_preserve_escaped_spaces(value), strip=True):
            attributes.append((attr.casefold(), _dn_value(part)))
            if sep != "+":
                rdns.append(tuple(sorted(attributes)))
                attributes = []
        return tuple(rdns)
    except Exception as exc:
        raise ValueError("Некорректный DN области поиска") from exc


def normalize_bases(values: list[str]) -> list[str]:
    result, seen = [], set()
    for value in values:
        dn = _preserve_escaped_spaces(value).strip()
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
