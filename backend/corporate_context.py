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
SYNC_LEASE_SECONDS = 45
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(value: str) -> str:
    return " ".join(_WORDS.findall(value.casefold().replace("ё", "е")))


def _read() -> dict:
    return {"enabled": True, "use_ad": True, "sync_ad": True, "sync_time": DEFAULT_SYNC_TIME, "revision": 0, "terms": [], "people": [],
            **(store.get_app_setting("corporate_context") or {})}


def _write(conn, data: dict) -> None:
    data["revision"] = data.get("revision", 0) + 1
    conn.execute("INSERT INTO app_settings(key,payload) VALUES ('corporate_context',?) "
                 "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                 (json.dumps(data, ensure_ascii=False),))
    # A tiny cross-process invalidation key avoids decoding a 50k-person JSON on every keystroke.
    conn.execute("INSERT INTO app_settings(key,payload) VALUES ('corporate_context_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (json.dumps(secrets.token_hex(16)),))


def _update(**changes) -> dict:
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM app_settings WHERE key='corporate_context'").fetchone()
        data = json.loads(row[0]) if row else {}
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
    job = _sync_job(data)
    signature = directory_signature()
    matches = data.get("directory_signature") == signature
    now = time.time()
    next_sync = _next_sync_at({**data, **job}, now, signature) if data["sync_ad"] and config.ldap_configured() else None
    interrupted = job.get("status") == "running" and job.get("sync_until", 0) <= now
    progress = {**job.get("progress", {}), "status": "interrupted" if interrupted else job.get("status", "idle"),
                "started_at": job.get("started_at"), "finished_at": job.get("finished_at")}
    needle = normalize(query)
    terms = [entry for entry in data["terms"] if needle in normalize(" ".join(
        [entry["term"], *entry["aliases"], entry["description"], entry["department"]]))]
    return {"enabled": data["enabled"], "use_ad": data["use_ad"], "sync_ad": data["sync_ad"], "revision": data["revision"],
            "sync_time": data["sync_time"], "sync_timezone": "UTC+3",
            "directory_policy": directory_scope.sync_policy(),
            "search_mode": directory_scope.speaker_search_mode(),
            "search_cache_ready": bool(matches and data.get("synced_at") and config.ldap_configured()),
            "next_sync_at": datetime.fromtimestamp(max(now, next_sync), timezone.utc).isoformat() if next_sync is not None else None,
            "term_count": len(data["terms"]), "people_count": len(data["people"]) if matches else 0,
            "directory_current": matches, "ldap_configured": config.ldap_configured(),
            "synced_at": data.get("synced_at"), "imported_at": data.get("imported_at"),
            "sync_error": "Обновление прервано: процесс перестал отвечать. Запустите обновление снова; предыдущий кэш сохранён."
                          if interrupted else job.get("sync_error"),
            "syncing": job.get("status") == "queued" or (job.get("sync_until", 0) > now),
            "sync_progress": progress,
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


def save_settings(enabled: bool | None, sync_ad: bool | None, actor: str, *, sync_time: str | None = None,
                  use_ad: bool | None = None) -> dict:
    if sync_time is not None and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", sync_time):
        raise ValueError("Укажите время синхронизации в формате ЧЧ:ММ")
    data = _read()
    changes = {key: value for key, value in {"enabled": enabled, "sync_ad": sync_ad, "use_ad": use_ad}.items()
               if value is not None}
    if sync_time is not None:
        changes["sync_time"] = sync_time
    if (sync_time is not None and sync_time != data["sync_time"]) or (sync_ad and not data["sync_ad"]):
        changes["schedule_updated_at"] = time.time()
    _update(**changes)
    store.record_audit(actor, "ldap.schedule" if enabled is None else "context.settings",
                       detail=json.dumps(changes, ensure_ascii=False))
    return snapshot()


def _next_sync_at(data: dict, now: float, signature: str) -> float:
    """Calendar scheduling survives restarts and never drifts after a manual run."""
    changed = data.get("schedule_updated_at", 0)
    if data.get("sync_id"):
        return data.get("sync_until", now)
    last = data.get("last_attempt", 0) if data.get("attempt_signature") == signature else 0
    if data.get("sync_error") and last >= changed and last:
        failed_at = data.get("failed_at")
        if failed_at is None and data.get("finished_at"):
            # Existing jobs from before failed_at was introduced already retain
            # their completion timestamp. Do not restart them early on upgrade.
            failed_at = datetime.fromisoformat(data["finished_at"]).timestamp()
        return max(last, failed_at or last) + 900
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


def _sync_job(data: dict | None = None) -> dict:
    saved = store.get_app_setting("ldap_sync_job")
    if saved is not None:
        return saved
    # Carry scheduling/error information across upgrades; the last complete roster stays intact.
    data = data if data is not None else _read()
    return {key: data[key] for key in ("last_attempt", "attempt_signature", "sync_error", "sync_id", "sync_until")
            if key in data}


def _write_job(conn, job: dict) -> None:
    conn.execute("INSERT INTO app_settings(key,payload) VALUES ('ldap_sync_job',?) "
                 "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (json.dumps(job, ensure_ascii=False),))


def _update_job(token: str, **changes) -> bool:
    # Progress and heartbeat writes never copy the roster or invalidate the name-search index.
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = _sync_job()
        if job.get("sync_id") != token or job.get("status") != "running":
            return False
        job.update(changes)
        _write_job(conn, job)
    return True


def request_sync(actor: str) -> dict:
    if not config.ldap_configured():
        raise ValueError("Сначала настройте подключение AD в разделе «Каталог»")
    if not directory_scope.search_bases():
        raise ValueError("Выберите и сохраните области выгрузки AD")
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = _sync_job()
        if job.get("status") == "queued" or job.get("sync_until", 0) > time.time():
            raise ValueError("Синхронизация AD уже выполняется")
        job.update(status="queued", actor=actor, sync_id=None, sync_until=0, sync_error=None,
                   started_at=None, finished_at=None, failed_at=None,
                   progress={"phase": "queued", "received": 0, "pages": 0})
        _write_job(conn, job)
    store.record_audit(actor, "ldap.sync.request", detail="Обновление кэша запрошено")
    return snapshot()


def _claim_sync(scheduled: bool) -> tuple[str, str] | None:
    """Persistent queue plus a renewable lease shared by every web/worker process."""
    signature, now = directory_signature(), time.time()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        data, job = _read(), _sync_job()
        if job.get("sync_until", 0) > now:
            if scheduled:
                return None
            raise ValueError("Синхронизация AD уже выполняется")
        queued = job.get("status") == "queued"
        if scheduled and not queued:
            if not data["sync_ad"] or not config.ldap_configured():
                return None
            if now < _next_sync_at({**data, **job}, now, signature):
                return None
        token = secrets.token_hex(16)
        job.update(last_attempt=now, attempt_signature=signature, sync_id=token,
                   sync_until=now + SYNC_LEASE_SECONDS, status="running", sync_error=None,
                   actor=job.get("actor", "system") if queued else "system",
                   started_at=store.utc_now(), finished_at=None, failed_at=None,
                   progress={"phase": "connecting", "received": 0, "pages": 0})
        _write_job(conn, job)
    return token, signature


def _publish_directory(token: str, signature: str, people: list[dict]) -> bool:
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = _sync_job()
        if job.get("sync_id") != token or job.get("status") != "running":
            return False
        if signature != directory_signature():
            raise ValueError("Настройки AD изменились во время синхронизации. Запустите её снова")
        data = _read()
        data.update(people=people, directory_signature=signature, synced_at=store.utc_now(), sync_error=None)
        _write(conn, data)
        job.update(status="succeeded", sync_id=None, sync_until=0, finished_at=store.utc_now(), sync_error=None, failed_at=None)
        job["progress"] = {**job.get("progress", {}), "phase": "done", "received": len(people)}
        _write_job(conn, job)
    return True


def sync_directory(actor: str = "system", *, scheduled: bool = False) -> dict:
    claim = _claim_sync(scheduled)
    if claim is None:
        return {}
    token, signature = claim
    done, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not done.wait(5):
            try:
                if not _update_job(token, sync_until=time.time() + SYNC_LEASE_SECONDS):
                    lost.set()
                    return
            except Exception:
                log.exception("Не удалось обновить статус выгрузки AD")
                lost.set()
                return

    def progress(state):
        if lost.is_set() or not _update_job(token, progress=state, sync_until=time.time() + SYNC_LEASE_SECONDS):
            raise ldap_auth.LdapError("Обновление кэша прервано: задача передана другому процессу")

    thread = threading.Thread(target=heartbeat, name="mom-ad-heartbeat", daemon=True)
    try:
        if scheduled:
            actor = _sync_job().get("actor", actor)
        thread.start()
        people = ldap_auth.sync_people(progress=progress)
        if lost.is_set():
            raise ldap_auth.LdapError("Обновление кэша прервано: потеряна связь с задачей")
        if _publish_directory(token, signature, people):
            store.record_audit(actor, "context.sync", detail=f"people={len(people)}")
    except Exception as exc:
        log.exception("Не удалось обновить корпоративный каталог")
        message = str(exc) if isinstance(exc, (ldap_auth.LdapError, ValueError)) else (
            f"Обновление кэша прервано ({type(exc).__name__}). Подробности — в журнале сервера MoM")
        if _update_job(token, status="error", sync_id=None, sync_until=0,
                       finished_at=store.utc_now(), failed_at=time.time(), sync_error=message):
            store.record_audit(actor, "ldap.sync.error", detail=message)
        raise
    finally:
        done.set()
        if thread.ident is not None:
            thread.join(timeout=1)
    return snapshot()


def _record_scheduler_failure(observed: dict | None, exc: Exception) -> None:
    """Best-effort status for failures before a worker could claim the job.

    Never overwrite a job claimed/completed by another worker in the meantime.
    If the database cannot be written, the caller's server log remains the
    authoritative diagnostic; reporting an error must not kill the scheduler.
    """
    try:
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = store.get_app_setting("ldap_sync_job")
            if current != observed:
                return
            job, now = _sync_job(), time.time()
            if job.get("sync_until", 0) > now:
                return
            message = (f"Не удалось запустить обновление AD ({type(exc).__name__}). "
                       "Проверьте журнал сервера MoM и доступность базы данных. Можно повторить запуск вручную")
            actor = job.get("actor", "system") if job.get("status") == "queued" else "system"
            job.update(status="error", sync_id=None, sync_until=0, sync_error=message,
                       actor=actor, started_at=None, last_attempt=now, failed_at=now, finished_at=store.utc_now(),
                       progress={"phase": "starting", "received": 0, "pages": 0},
                       attempt_signature=directory_signature())
            _write_job(conn, job)
        store.record_audit(job.get("actor", "system"), "ldap.sync.error", detail=message)
    except Exception:
        log.exception("Не удалось сохранить причину сбоя запуска AD; проверьте доступ к базе данных")


async def run_sync(stop: asyncio.Event) -> None:
    next_schedule_check = 0.0
    retry_after = 0.0
    while not stop.is_set():
        observed = None
        try:
            if time.monotonic() >= retry_after:
                observed = await asyncio.to_thread(store.get_app_setting, "ldap_sync_job")
                if (observed or {}).get("status") == "queued" or time.monotonic() >= next_schedule_check:
                    await asyncio.to_thread(sync_directory, scheduled=True)
                    next_schedule_check = time.monotonic() + 60
        except Exception as exc:
            log.exception("Сбой фонового обновления AD")
            await asyncio.to_thread(_record_scheduler_failure, observed, exc)
            retry_after = time.monotonic() + 60
            next_schedule_check = time.monotonic() + 60
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
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
    people = data["people"] if data["use_ad"] and config.ldap_configured() and data.get("directory_signature") == directory_signature() else []
    user = store.get_user(meeting["user_id"]) if meeting.get("user_id") else None
    return Context(data["terms"], people, data["revision"], (user or {}).get("email", ""))
