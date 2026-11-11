"""Versioned corporate vocabulary shared by recognition and protocol generation."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import heapq
import io
import json
import logging
import re
import secrets
import time
import threading
from xml.etree.ElementTree import ParseError
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zipfile import BadZipFile, ZipFile

from . import config, directory_scope, ldap_auth, store

log = logging.getLogger("mom.context")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TERMS = 5000
SYNC_TIMEZONE = timezone(timedelta(hours=3))
DEFAULT_SYNC_TIME = "03:00"
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(value: str) -> str:
    return " ".join(_WORDS.findall(value.casefold().replace("ё", "е")))


def _read() -> dict:
    return {"enabled": True, "sync_ad": True, "sync_time": DEFAULT_SYNC_TIME, "revision": 0, "terms": [], "people": [],
            **(store.get_app_setting("corporate_context") or {})}


def _write(conn, data: dict) -> None:
    data["revision"] = data.get("revision", 0) + 1
    conn.execute("INSERT INTO app_settings(key,payload) VALUES ('corporate_context',?) "
                 "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                 (json.dumps(data, ensure_ascii=False),))
    # A tiny cross-process invalidation key avoids decoding a 50k-person JSON on every keystroke.
    conn.execute("INSERT INTO app_settings(key,payload) VALUES ('corporate_context_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (json.dumps(secrets.token_hex(16)),))


def _update(*, _expected_sync_id: str | None = None, **changes) -> dict | None:
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM app_settings WHERE key='corporate_context'").fetchone()
        data = json.loads(row[0]) if row else {}
        if _expected_sync_id is not None and data.get("sync_id") != _expected_sync_id:
            return None
        data.update(changes)
        _write(conn, data)
    return data


def directory_signature() -> str:
    data = [config.ldap_configured(), config.get_ldap_url(), config.get_ldap_base_dn(),
            config.get_ldap_email_attr(), config.get_ldap_bind_dn(), directory_scope.search_bases()]
    policy = directory_scope.sync_policy()
    # Preserve existing complete snapshots on upgrade when the defaults are unchanged.
    if set(policy["fields"]) != set(directory_scope.OPTIONAL_FIELDS) or policy["domains"]:
        data.append(policy)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def snapshot(query: str = "") -> dict:
    data = _read()
    signature = directory_signature()
    matches = data.get("directory_signature") == signature
    now = time.time()
    next_sync = _next_sync_at(data, now, signature) if data["sync_ad"] and config.ldap_configured() else None
    needle = normalize(query)
    terms = [entry for entry in data["terms"] if needle in normalize(" ".join(
        [entry["term"], *entry["aliases"], entry["description"], entry["department"]]))]
    return {"enabled": data["enabled"], "sync_ad": data["sync_ad"], "revision": data["revision"],
            "sync_time": data["sync_time"], "sync_timezone": "UTC+3",
            "directory_policy": directory_scope.sync_policy(),
            "search_mode": directory_scope.speaker_search_mode(),
            "search_cache_ready": bool(matches and data.get("synced_at") and config.ldap_configured()),
            "next_sync_at": datetime.fromtimestamp(max(now, next_sync), timezone.utc).isoformat() if next_sync is not None else None,
            "term_count": len(data["terms"]), "people_count": len(data["people"]) if matches else 0,
            "directory_current": matches, "ldap_configured": config.ldap_configured(),
            "synced_at": data.get("synced_at"), "imported_at": data.get("imported_at"),
            "sync_error": data.get("sync_error"), "syncing": data.get("sync_until", 0) > time.time(),
            "terms": terms[:100], "matched_terms": len(terms)}


def save_search_mode(mode: str, actor: str) -> dict:
    if mode not in {"live", "cache"}:
        raise ValueError("Выберите живой AD или локальный кэш")
    if mode == "cache" and not snapshot()["search_cache_ready"]:
        raise ValueError("Сначала успешно синхронизируйте AD с текущими настройками")
    # Separate from the context snapshot: switching never rebuilds or invalidates the cache.
    store.set_app_setting("ldap_speaker_search", {"mode": mode})
    store.record_audit(actor, "context.search", detail="Живой AD" if mode == "live" else "Локальный кэш AD")
    return snapshot()


_people_cache_lock = threading.Lock()
_people_cache: tuple | None = None


def search_people(query: str, limit: int = 20) -> tuple[list[dict], str | None]:
    """Search the last complete snapshot, never LDAP. Scope changes fail closed immediately."""
    global _people_cache
    signature = directory_signature()
    version = store.get_app_setting("corporate_context_version")
    key = (str(store.DB_PATH), version, signature)
    with _people_cache_lock:
        if _people_cache is None or _people_cache[0] != key:
            data = _read()
            current = data.get("directory_signature") == signature
            index = [(normalize(person["name"] + " " + person.get("email", "")), person)
                     for person in data["people"]] if current else []
            _people_cache = (key, current, index)
        _, current, index = _people_cache
    if not current:
        return [], "Справочник AD ещё не обновлён. Администратор может запустить синхронизацию; пока введите имя вручную."
    parts = normalize(query).split()
    if not parts:
        return [], None
    needle = normalize(query)
    matches = (person for text, person in index if all(part in text for part in parts))
    return [dict(person) for person in heapq.nsmallest(limit, matches,
            key=lambda person: (not normalize(person["name"]).startswith(needle), person["name"].casefold()))], None


def save_settings(enabled: bool, sync_ad: bool, actor: str, *, sync_time: str | None = None) -> dict:
    if sync_time is not None and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", sync_time):
        raise ValueError("Укажите время синхронизации в формате ЧЧ:ММ")
    data = _read()
    changes = {"enabled": enabled, "sync_ad": sync_ad}
    if sync_time is not None:
        changes["sync_time"] = sync_time
    if (sync_time is not None and sync_time != data["sync_time"]) or (sync_ad and not data["sync_ad"]):
        changes["schedule_updated_at"] = time.time()
    _update(**changes)
    store.record_audit(actor, "context.settings", detail=f"enabled={enabled}, sync_ad={sync_ad}, time={sync_time or data['sync_time']} UTC+3")
    return snapshot()


def _next_sync_at(data: dict, now: float, signature: str) -> float:
    """Calendar scheduling survives restarts and never drifts after a manual run."""
    changed = data.get("schedule_updated_at", 0)
    if data.get("sync_id"):
        return data.get("sync_until", now)
    last = data.get("last_attempt", 0) if data.get("attempt_signature") == signature else 0
    if data.get("sync_error") and last >= changed and last:
        return last + 900
    hour, minute = map(int, data.get("sync_time", DEFAULT_SYNC_TIME).split(":"))
    target = datetime.fromtimestamp(now, SYNC_TIMEZONE).replace(hour=hour, minute=minute, second=0, microsecond=0)
    # A newly selected time starts at its next occurrence, including across midnight.
    if target.timestamp() <= changed or target.timestamp() <= last:
        target += timedelta(days=1)
    return target.timestamp()


def parse_dictionary(filename: str, content: bytes) -> list[dict]:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Словарь должен быть не больше 2 МБ")
    if filename.lower().endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        try:
            return _parse_rows(csv.reader(io.StringIO(text), dialect))
        except csv.Error as exc:
            raise ValueError("Не удалось прочитать CSV. Проверьте разделители и длину полей") from exc
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("Выберите словарь Excel (.xlsx) или CSV")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024:
                raise ValueError("Распакованный Excel превышает 20 МБ")
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            sheet = book.active
            if sheet.max_column and sheet.max_column > 20:
                raise ValueError("В словаре должно быть не больше 20 столбцов")
            sheet.reset_dimensions()
            def rows():
                for row in sheet.iter_rows(max_col=20):
                    if any(cell.data_type == "f" for cell in row):
                        raise ValueError("Замените формулы в словаре готовыми значениями")
                    yield [cell.value for cell in row]
            return _parse_rows(rows())
        finally:
            book.close()
    except (BadZipFile, KeyError, OSError, ParseError) as exc:
        raise ValueError("Не удалось прочитать Excel-файл") from exc


def _parse_rows(rows) -> list[dict]:
    rows = iter(rows)
    header = next(rows, [])
    names = {"термин": "term", "варианты": "aliases", "описание": "description", "подразделение": "department"}
    columns = [names.get(str(value or "").strip().casefold(), str(value or "").strip().casefold()) for value in header]
    if "term" not in columns or len(set(filter(None, columns))) != len(list(filter(None, columns))):
        raise ValueError("Нужен столбец «Термин». Названия столбцов должны быть уникальными")
    entries = {}
    for number, row in enumerate(rows, 2):
        if number > MAX_TERMS + 1:
            raise ValueError("В словаре допускается до 5000 строк")
        if not any(value is not None and str(value).strip() for value in row):
            continue
        item = {column: " ".join(str(value or "").split()) for column, value in zip(columns, row) if column}
        term = item.get("term", "")
        aliases = list(dict.fromkeys(part.strip() for part in item.get("aliases", "").split("|") if part.strip()))
        department, description = item.get("department", ""), item.get("description", "")
        if not normalize(term) or len(term) > 120 or len(department) > 120 or len(description) > 500:
            raise ValueError(f"Строка {number}: проверьте термин и длину полей (120 / 120 / 500 символов)")
        if len(aliases) > 20 or any(not normalize(alias) or len(alias) > 120 for alias in aliases):
            raise ValueError(f"Строка {number}: допускается до 20 вариантов длиной до 120 символов через |")
        key = (normalize(term), normalize(department))
        if key in entries:
            raise ValueError(f"Строка {number}: термин уже указан для этого подразделения")
        entries[key] = {"term": term, "aliases": aliases, "department": department, "description": description}
    if not entries:
        raise ValueError("Словарь пуст")
    return list(entries.values())


def import_dictionary(filename: str, content: bytes, actor: str) -> dict:
    entries = parse_dictionary(filename, content)
    _update(terms=entries, imported_at=store.utc_now())
    store.record_audit(actor, "context.import", detail=f"terms={len(entries)}")
    return snapshot()


def _claim_sync(scheduled: bool) -> tuple[str, str] | None:
    """Coordinate the web process and workers; a crashed worker releases its lease in 2 minutes."""
    signature, now = directory_signature(), time.time()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM app_settings WHERE key='corporate_context'").fetchone()
        data = json.loads(row[0]) if row else {}
        if data.get("sync_until", 0) > now:
            if scheduled:
                return None
            raise ValueError("Синхронизация AD уже выполняется")
        if scheduled and (not data.get("sync_ad", True) or not config.ldap_configured()):
            return None
        if scheduled and now < _next_sync_at(data, now, signature):
            return None
        token = secrets.token_hex(16)
        data.update(last_attempt=now, attempt_signature=signature, sync_id=token, sync_until=now + 120)
        _write(conn, data)
    return token, signature


def sync_directory(actor: str = "system", *, scheduled: bool = False) -> dict:
    claim = _claim_sync(scheduled)
    if claim is None:
        return {}
    token, signature = claim
    try:
        people = ldap_auth.sync_people()
        if signature != directory_signature():
            raise ValueError("Настройки AD изменились во время синхронизации. Запустите её снова")
        if _update(_expected_sync_id=token, people=people, directory_signature=signature,
                   synced_at=store.utc_now(), sync_error=None) is not None:
            store.record_audit(actor, "context.sync", detail=f"people={len(people)}")
    except (ldap_auth.LdapError, ValueError) as exc:
        _update(_expected_sync_id=token, sync_error=str(exc))
        raise
    finally:
        _update(_expected_sync_id=token, sync_id=None, sync_until=0)
    return snapshot()


async def run_sync(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(sync_directory, scheduled=True)
        except Exception:
            log.exception("Не удалось обновить корпоративный каталог")
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass


class Context:
    def __init__(self, terms: list[dict], people: list[dict], revision: int, email: str = ""):
        self.revision = revision
        self.entries = [dict(entry, source="dictionary") for entry in terms]
        self.entries.extend({"term": person["name"], "aliases": [], "source": "ad",
                             "description": person.get("job_title", ""), "department": person.get("department", "")}
                            for person in people)
        self.department = next((normalize(p.get("department", "")) for p in people
                                if p.get("email", "").casefold() == email.casefold()), "")
        self.index: dict[str, set[int]] = defaultdict(set)
        self.base_scores = {}
        self.alias_targets: dict[str, set[str]] = defaultdict(set)
        for index, entry in enumerate(self.entries):
            for phrase in [entry["term"], *entry["aliases"]]:
                self.index[normalize(phrase)].add(index)
                if entry["source"] == "dictionary":
                    self.alias_targets[normalize(phrase)].add(entry["term"])
            if entry["source"] == "dictionary":
                department = normalize(entry["department"])
                if department and department == self.department:
                    self.base_scores[index] = 2
                elif not department:
                    self.base_scores[index] = 1
        # A surname only selects context when it identifies exactly one directory entry.
        surnames: dict[str, list[int]] = defaultdict(list)
        for index, person in enumerate(people, len(terms)):
            surname = normalize(person.get("surname", ""))
            if surname:
                surnames[surname].append(index)
        for surname, indices in surnames.items():
            if len(indices) == 1:
                self.index[surname].add(indices[0])
        self.lengths = sorted({len(phrase.split()) for phrase in self.index if len(phrase.split()) <= 12})

    def select(self, text: str = "", limit: int = 30) -> list[dict]:
        words = normalize(text).split()
        scores = dict(self.base_scores)
        matches = set()
        for length in self.lengths:
            for start in range(len(words) - length + 1):
                matches.update(self.index.get(" ".join(words[start:start + length]), ()))
        for index in matches:
            scores[index] = scores.get(index, 0) + 10
        ranked = heapq.nsmallest(limit, scores, key=lambda index: (-scores[index], self.entries[index]["term"]))
        return [self.entries[index] for index in ranked]

    def hotwords(self, text: str = "") -> str:
        selected = self.select(text)
        words, size = [], 0
        for entry in selected:
            term = entry["term"]
            if size + len(term.encode()) + 2 > 500:
                continue
            words.append(term)
            size += len(term.encode()) + 2
        return ", ".join(words)

    def apply(self, document: dict) -> dict:
        """Only unambiguous, administrator-supplied spellings are normalized."""
        text = " ".join(segment["text"] for segment in document.get("segments", []))
        selected = self.select(text)
        replacements = {}
        for entry in selected:
            for alias in entry["aliases"]:
                if len(self.alias_targets[normalize(alias)]) == 1 and normalize(alias) != normalize(entry["term"]):
                    replacements[alias.casefold()] = entry["term"]
        segments = [dict(segment) for segment in document.get("segments", [])]
        if replacements:
            pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(alias) for alias in
                                 sorted(replacements, key=len, reverse=True)) + r")(?!\w)", re.IGNORECASE)
            for segment in segments:
                original = segment["text"]
                updated = pattern.sub(lambda match: replacements[match[0].casefold()], original)
                if updated != original:
                    segment.setdefault("context_original_text", original)
                    segment["text"] = updated
        return {**document, "segments": segments,
                "corporate_context": {"revision": self.revision, "entries": selected}}


def for_meeting(meeting: dict) -> Context:
    data = _read()
    if not data["enabled"]:
        return Context([], [], data["revision"])
    people = data["people"] if config.ldap_configured() and data.get("directory_signature") == directory_signature() else []
    user = store.get_user(meeting["user_id"]) if meeting.get("user_id") else None
    return Context(data["terms"], people, data["revision"], (user or {}).get("email", ""))
