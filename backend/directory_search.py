"""Bounded live directory lookups; separate from the administrator's AD dataset."""
from collections import OrderedDict, deque
from concurrent.futures import Future, TimeoutError
import copy
import hashlib
import json
import threading
import time

from . import config, directory_scope, ldap_auth, store

TTL_SECONDS = 30
MAX_CACHE = 256
MAX_INFLIGHT = 4
MAX_WAITERS = 8
MAX_PRINCIPALS = 2048
REQUESTS_PER_MINUTE = 120
_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
_pending: dict[tuple, Future] = {}
_rates: OrderedDict[str, deque] = OrderedDict()
_generation = 0
_waiters = 0


def invalidate() -> None:
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()


def allow(principal: str) -> bool:
    now = time.monotonic()
    with _lock:
        hits = _rates.setdefault(principal, deque())
        _rates.move_to_end(principal)
        while len(_rates) > MAX_PRINCIPALS:
            _rates.popitem(last=False)
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= REQUESTS_PER_MINUTE:
            return False
        hits.append(now)
        return True


def _revision(for_speakers: bool) -> str:
    values = [_generation, str(store.DB_PATH), config.ldap_configured(), config.get_ldap_url(),
              config.get_ldap_bind_dn(), config.get_ldap_bind_password(), config.get_ldap_base_dn(),
              config.get_ldap_starttls(), config.get_ldap_tls_verify(), config.get_ldap_email_attr(),
              directory_scope.search_bases(), directory_scope.sync_policy() if for_speakers else None]
    # A saved password change invalidates entries without keeping credentials in
    # cache keys or exposing them in diagnostics.
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def search(query: str, *, for_speakers: bool = False) -> list[dict]:
    global _waiters
    needle = query.strip()
    if len(needle) < 2 or not config.ldap_configured():
        return []
    revision = _revision(for_speakers)
    key = revision, for_speakers, needle.casefold()
    now = time.monotonic()
    with _lock:
        for old, (expires, _) in list(_cache.items()):
            if expires <= now:
                _cache.pop(old, None)
        cached = _cache.get(key)
        if cached:
            _cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        future = _pending.get(key)
        owner = future is None
        if owner:
            if len(_pending) >= MAX_INFLIGHT:
                raise ldap_auth.LdapError('Каталог занят. Повторите поиск через несколько секунд.')
            future = _pending[key] = Future()
        else:
            # Coalesced requests must not occupy the whole HTTP thread pool.
            if _waiters >= MAX_WAITERS:
                raise ldap_auth.LdapError('Каталог занят. Повторите поиск через несколько секунд.')
            _waiters += 1
    if not owner:
        try:
            result = future.result(timeout=15)
        except TimeoutError as exc:
            raise ldap_auth.LdapError('Поиск в AD ещё выполняется. Повторите запрос позже.') from exc
        finally:
            with _lock:
                _waiters -= 1
        if _revision(for_speakers) != revision:
            raise ldap_auth.LdapError('Настройки каталога изменились. Повторите поиск.')
        return copy.deepcopy(result)
    try:
        result = ldap_auth.search_people(needle, **({"for_speakers": True} if for_speakers else {}))
        if _revision(for_speakers) != revision:
            raise ldap_auth.LdapError('Настройки каталога изменились. Повторите поиск.')
        with _lock:
            _cache[key] = (time.monotonic() + TTL_SECONDS, copy.deepcopy(result))
            while len(_cache) > MAX_CACHE:
                _cache.popitem(last=False)
        future.set_result(result)
        return copy.deepcopy(result)
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _lock:
            _pending.pop(key, None)
