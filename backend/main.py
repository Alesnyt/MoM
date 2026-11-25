from __future__ import annotations

import asyncio
import json
import errno
import hashlib
import logging
import sqlite3
import threading
import uuid
import time
from typing import Literal
from pathlib import Path
from urllib.parse import quote

from contextlib import asynccontextmanager

import anyio
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from . import directory_search, protocol_editor, remote_asr, request_origin
from .request_limits import RequestLimitsMiddleware
from pydantic import ValidationError
from .api_schemas import (
    SettingsIn,
    AsrApiIn,
    AsrSettingsIn,
    ContextSettingsIn,
    GlossaryImportIn,
    GlossaryEnabledIn,
    TermScheduleIn,
    TermReviewItem,
    TermReviewIn,
    DirectorySettingsIn,
    DirectoryPolicyIn,
    SpeakerSearchIn,
    PromptsIn,
    ModelsIn,
    ThemeIn,
    SmtpIn,
    QueueIn,
    LdapIn,
    DirectoryScopeIn,
    LdapTestIn,
    SpeakerNameIn,
    ProtocolSendIn,
    MergeSpeakersIn,
    ProtocolIn,
    SpeakerAssignIn,
    SmtpTestIn,
    LoginIn,
    UserLoginIn,
    UserCreateIn,
    UserPatchIn
)

from . import audit, auth, config, diagnostics, directory_scope, disk, keys, ldap_auth, monitoring, people, performance, prompts, retention, store, worker
from .audio import AudioError, audio_source, ffmpeg_available, looks_like_audio, playback_clip
from .analyze import ProtocolResult, StoredProtocolResult
from . import transcript_import, upload, corporate_context, glossary, term_discovery
from . import word_protocol, context_api, calendar_api, calendar_service
from .exchange_ews import ExchangeError
from starlette.datastructures import UploadFile as StarletteUploadFile
from .config import AUDIO_DIR, UPLOAD_DIR, ensure_dirs
from .mail import prepare_protocol_mail, queue_protocol_mail, build_eml, email_body, send_mail, smtp_snapshot
from .pipeline import format_duration
from .speakers import SpeakerError, clean_speaker_email, merge_speakers, participant_labels, reassign_segments, rename_speaker, render_transcript, replace_speaker_label

log = logging.getLogger("mom.api")

ALLOWED_SUFFIXES = {".webm", ".mkv", ".mp4", ".mp3", ".wav", ".m4a", ".ogg"}
STATIC_DIR = Path(__file__).resolve().parent / "static"
_setup_lock = threading.Lock()
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient", "localhost"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
}


def _peer_trusted(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in _LOOPBACK or host in config.TRUSTED_PROXY_IPS


def _client_ip(request: Request) -> str:
    peer = (request.client.host if request.client else "") or "unknown"
    if _peer_trusted(request):
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer


def _cookie_kw(request: Request) -> dict:
    secure = request.url.scheme == "https"
    if _peer_trusted(request):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            secure = True
    return {
        "httponly": True,
        "samesite": "lax",
        "max_age": auth.SESSION_SECONDS,
        "path": "/",
        "secure": secure,
    }


def _clear_cookie(response: Response, name: str, request: Request) -> None:
    kw = _cookie_kw(request)
    response.delete_cookie(name, path="/", secure=kw["secure"], httponly=True, samesite="lax")


def _rate_or_429(request: Request, action: str) -> None:
    if not auth.rate_allow(f"{action}:{_client_ip(request)}"):
        raise HTTPException(status_code=429, detail="Слишком много попыток, подождите минуту", headers={"Retry-After": "60"})


async def _early_auth_rate(request: Request, action: str) -> None:
    # The persistent limiter uses SQLite; keep it off the event loop and run
    # before accepting a login/setup body, including invalid JSON.
    await run_in_threadpool(_rate_or_429, request, action)


def _search_rate_or_429(principal: str) -> None:
    if not directory_search.allow(principal):
        raise HTTPException(429, "Слишком много запросов поиска. Подождите минуту.", headers={"Retry-After": "60"})


def _assert_setup_allowed(request: Request, token: str) -> None:
    expected = config.get_setup_token()
    if expected:
        if not auth.tokens_match(token, expected):
            raise HTTPException(
                status_code=403,
                detail="Нужен токен установки SETUP_TOKEN из файла .env",
            )
        return
    if _client_ip(request) not in _LOOPBACK:
        raise HTTPException(
            status_code=403,
            detail="Задайте SETUP_TOKEN в .env или создайте администратора с localhost",
        )


def require_admin(mom_admin: str | None = Cookie(default=None)) -> dict:
    session = auth.get_session(mom_admin)
    if not session:
        raise HTTPException(status_code=401, detail="Нужна авторизация администратора")
    return session


def require_user(mom_user: str | None = Cookie(default=None)) -> dict:
    session = auth.get_user_session(mom_user)
    if not session:
        raise HTTPException(status_code=401, detail="Войдите в профиль")
    return session


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _valid_email(value: str) -> bool:
    if "@" not in value or " " in value:
        return False
    local, _, domain = value.partition("@")
    return bool(local and "." in domain)


def _owned(meeting: dict | None, user: dict) -> dict:
    if not meeting or meeting.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    return meeting


def ldap_snapshot(*, full: bool = False) -> dict:
    data = {"enabled": config.ldap_enabled(), "configured": config.ldap_configured()}
    if not full:
        return data
    data.update(
        {
            "url": config.get_ldap_url() or None,
            "bind_dn": config.get_ldap_bind_dn() or None,
            "has_password": bool(config.get_ldap_bind_password()),
            "base_dn": config.get_ldap_base_dn() or None,
            "user_filter": config.get_ldap_user_filter(),
            "starttls": config.get_ldap_starttls(),
            "tls_verify": config.get_ldap_tls_verify(),
            "email_attr": config.get_ldap_email_attr(),
        }
    )
    return data


def _check_ldap_settings(payload: LdapIn) -> None:
    url = payload.url.strip()
    filt = payload.user_filter.strip() or "(mail={username})"
    if payload.enabled:
        if not url.startswith(("ldap://", "ldaps://")):
            raise HTTPException(status_code=400, detail="Адрес каталога должен начинаться с ldap:// или ldaps://")
        if not payload.base_dn.strip():
            raise HTTPException(status_code=400, detail="Укажите базу поиска, например ou=people,dc=example,dc=com")
        if "{username}" not in filt:
            raise HTTPException(status_code=400, detail="В фильтре должен быть плейсхолдер {username}")


def _actor_name(session: dict) -> str:
    return (session.get("username") or session.get("email") or "система").strip() or "система"


def _normalize_auth_mode(value: str) -> str:
    mode = (value or "local").strip().lower()
    if mode not in {"local", "ldap"}:
        raise HTTPException(status_code=400, detail="Режим входа: local или ldap")
    if mode == "ldap" and not config.ldap_configured():
        raise HTTPException(status_code=400, detail="Сначала сохраните и включите LDAP в админке")
    return mode


def _health(*, full: bool = False) -> dict:
    if full:
        disk_status = disk.snapshot()
    else:
        disk_status = {"ok": disk.has_room()}
    return {
        "ok": worker.snapshot()["ok"],
        "worker": worker.snapshot(),
        "ffmpeg": ffmpeg_available(),
        "ui": (STATIC_DIR / "index.html").exists(),
        "theme": config.get_ui_theme(),
        "max_upload_bytes": config.MAX_UPLOAD_BYTES,
        "openai": keys.snapshot() if full else keys.public_snapshot(),
        **({"asr": config.asr_admin_settings()} if full else {}),
        "queue": store.queue_stats(),
        "smtp": smtp_snapshot(full=full),
        "ldap": ldap_snapshot(full=full),
        "disk": disk_status,
    }

@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    store.init_db()
    await run_in_threadpool(retention.reconcile_legacy_uploads, ALLOWED_SUFFIXES)
    await run_in_threadpool(diagnostics.initialize)
    store.requeue_interrupted_meetings()
    config.migrate_token_plan_asr()
    if config.get_api_key():
        try:
            await asyncio.wait_for(keys.verify_key(), timeout=float(config.VERIFY_TIMEOUT_SECONDS) + 5)
        except Exception:
            keys.note_check_failed("Не удалось связаться с провайдером при запуске.")
    else:
        keys.reset_status()
    stop = asyncio.Event()
    worker_task = asyncio.create_task(worker.run_worker(stop), name="mom-queue")
    try:
        yield
    finally:
        stop.set()
        worker.notify_work()
        try:
            await asyncio.wait_for(worker_task, timeout=4)
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="MoM", version="1.0.0", lifespan=lifespan)
@app.exception_handler(config.SettingsSaveError)
async def settings_save_error(_request: Request, exc: config.SettingsSaveError):
    log.error("Не удалось сохранить настройки", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail":
        "Не удалось записать настройки. Работающая конфигурация не изменена. "
        "Проверьте права на файл настроек и свободное место."})


app.add_middleware(RequestLimitsMiddleware, rate_check=_early_auth_rate)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if not request_origin.allowed(request, trusted_proxy=_peer_trusted(request)):
        response = JSONResponse({"detail": "Запрос с другого сайта отклонён. Откройте MoM и повторите действие."}, status_code=403)
    else:
        response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.get("/api/health")
def health(response: Response) -> dict:
    state = _health(full=False)
    if not state["ok"]:
        response.status_code = 503
    return state


app.include_router(context_api.create_router(require_admin))
app.include_router(calendar_api.create_router(require_user, require_admin, _rate_or_429))


@app.get("/api/auth/status")
def auth_status(mom_admin: str | None = Cookie(default=None)) -> dict:
    return auth.status(mom_admin)


@app.post("/api/auth/setup")
def auth_setup(payload: LoginIn, request: Request, response: Response) -> dict:
    with _setup_lock:
        if config.admin_configured():
            raise HTTPException(status_code=409, detail="Учётная запись администратора уже создана")
        _assert_setup_allowed(request, payload.setup_token)
        username = payload.username.strip()
        password = payload.password
        if len(username) < 3:
            raise HTTPException(status_code=400, detail="Логин должен быть не короче 3 символов")
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="Пароль должен быть не короче 8 символов")
        config.set_admin_credentials(username, auth.hash_password(password))
        token = auth.create_session(username)
        store.record_audit(username, "admin.setup", username, "создана учётная запись администратора")
    response.set_cookie(auth.COOKIE_NAME, token, **_cookie_kw(request))
    return {"configured": True, "authenticated": True, "username": username, "setup_token_required": False}


@app.post("/api/auth/login")
def auth_login(payload: LoginIn, request: Request, response: Response) -> dict:
    if not config.admin_configured():
        raise HTTPException(status_code=400, detail="Сначала создайте учётную запись администратора")
    username = payload.username.strip()
    if not auth.check_credentials(username, payload.password):
        store.record_audit(username or "администратор", "admin.login.fail", username, "неверный логин или пароль")
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = auth.create_session(username)
    store.record_audit(username, "admin.login", username, "вход в админку")
    response.set_cookie(auth.COOKIE_NAME, token, **_cookie_kw(request))
    return {"configured": True, "authenticated": True, "username": username, "setup_token_required": False}


@app.post("/api/auth/logout")
def auth_logout(
    request: Request,
    response: Response,
    mom_admin: str | None = Cookie(default=None),
) -> dict:
    auth.drop_session(mom_admin)
    _clear_cookie(response, auth.COOKIE_NAME, request)
    return {"configured": config.admin_configured(), "authenticated": False, "username": None}


@app.get("/api/session")
def user_session(mom_user: str | None = Cookie(default=None)) -> dict:
    session = auth.get_user_session(mom_user)
    if not session:
        return {"authenticated": False, "email": None, "archive_limit": None}
    user = store.get_user(session["user_id"])
    if not user:
        return {"authenticated": False, "email": None, "archive_limit": None}
    return {
        "authenticated": True,
        "id": user["id"],
        "email": user["email"],
        "archive_limit": user["archive_limit"],
        "meeting_count": user.get("meeting_count", 0),
    }


@app.post("/api/session/login")
def user_login(payload: UserLoginIn, request: Request, response: Response) -> dict:
    email = _normalize_email(payload.email)
    try:
        record = auth.authenticate_platform_user(email, payload.password)
    except ldap_auth.LdapError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not record:
        store.record_audit(email or "пользователь", "user.login.fail", email, "неверный email или пароль")
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    try:
        token = auth.create_user_session(record["id"], record["email"], record["auth_version"])
    except store.CredentialsChanged as exc:
        raise HTTPException(401, str(exc)) from exc
    user = store.get_user(record["id"])
    if user is None:
        raise HTTPException(401, "Профиль удалён. Войдите заново.")
    store.record_audit(record["email"], "user.login", record["email"], "вход в профиль")
    response.set_cookie(auth.USER_COOKIE, token, **_cookie_kw(request))
    # Only a successful LDAP login may hand its password to this session's
    # Exchange client. Failure of this optional integration must not reject login.
    try:
        calendar_service.start_ldap(token, record, payload.password)
    except Exception:
        log.warning("Автоподключение календаря не запущено; вход в MoM выполнен")
    return {
        "authenticated": True,
        "id": user["id"],
        "email": user["email"],
        "archive_limit": user["archive_limit"],
        "meeting_count": user.get("meeting_count", 0),
    }


@app.post("/api/session/logout")
def user_logout(
    request: Request,
    response: Response,
    mom_user: str | None = Cookie(default=None),
) -> dict:
    calendar_service.disconnect(mom_user)
    auth.drop_user_session(mom_user)
    _clear_cookie(response, auth.USER_COOKIE, request)
    return {"authenticated": False, "email": None, "archive_limit": None}


@app.get("/api/admin/audit")
def admin_audit(limit: int = 20, _admin: dict = Depends(require_admin)) -> list[dict]:
    if limit not in audit.PAGE_SIZES:
        raise HTTPException(status_code=400, detail="Можно показать 20, 40 или 100 записей")
    return audit.page(limit)


@app.get("/api/admin/audit/export")
def admin_audit_export(
    fmt: str = Query(default="csv", alias="format"),
    _admin: dict = Depends(require_admin),
) -> Response:
    kind = fmt.strip().lower()
    if kind == "csv":
        body = audit.export_csv()
        media = "text/csv; charset=utf-8"
        filename = "mom-zhurnal.csv"
    elif kind == "xlsx":
        body = audit.export_xlsx()
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "mom-zhurnal.xlsx"
    else:
        raise HTTPException(status_code=400, detail="Формат: csv или xlsx")
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/users")
def admin_list_users(_admin: dict = Depends(require_admin)) -> list[dict]:
    return store.list_users()


@app.get("/api/admin/directory")
def admin_directory(q: str = Query(default="", max_length=200), _admin: dict = Depends(require_admin)) -> dict:
    _search_rate_or_429("admin:" + _admin["username"])
    return people.directory(q)


@app.post("/api/admin/users")
def admin_create_user(payload: UserCreateIn, _admin: dict = Depends(require_admin)) -> dict:
    email = _normalize_email(payload.email)
    if not _valid_email(email):
        raise HTTPException(status_code=400, detail="Укажите корректный email")
    mode = _normalize_auth_mode(payload.auth_mode)
    if store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="Пользователь с таким email уже есть")
    password = None if mode == "ldap" else auth.generate_password()
    password_hash = auth.ldap_password_placeholder() if mode == "ldap" else auth.hash_password(password or "")
    try:
        user = store.create_user(email, password_hash, payload.archive_limit, mode)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Пользователь с таким email уже есть")
    store.record_audit(
        _actor_name(_admin),
        "user.create",
        email,
        f"вход {mode}, архив {payload.archive_limit}",
    )
    return {"user": user, "password": password}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: str,
    payload: UserPatchIn,
    _admin: dict = Depends(require_admin),
) -> dict:
    current = store.get_user(user_id)
    if not current:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    fields: dict = {}
    generated = None
    actor = _actor_name(_admin)
    if payload.archive_limit is not None:
        fields["archive_limit"] = payload.archive_limit
    if payload.auth_mode is not None:
        mode = _normalize_auth_mode(payload.auth_mode)
        if mode != current["auth_mode"]:
            fields["auth_mode"] = mode
            if mode == "ldap":
                fields["password_hash"] = auth.ldap_password_placeholder()
            else:
                generated = auth.generate_password()
                fields["password_hash"] = auth.hash_password(generated)
    if fields:
        try:
            updated = store.update_user(user_id, expected_mode=current["auth_mode"], **fields)
        except store.CredentialsChanged as exc:
            raise HTTPException(409, str(exc)) from exc
        if updated is None:
            raise HTTPException(404, "Пользователь не найден")
        if "auth_mode" in fields:
            store.record_audit(actor, "user.auth", current["email"],
                               f"{current['auth_mode']} → {fields['auth_mode']}")
        if "archive_limit" in fields and fields["archive_limit"] != current["archive_limit"]:
            store.record_audit(actor, "user.limit", current["email"],
                               f"архив {current['archive_limit']} → {fields['archive_limit']}")
    if payload.archive_limit is not None:
        retention.trim_archive(user_id, actor)
    user = store.get_user(user_id)
    if generated:
        return {"user": user, "password": generated}
    return user


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, _admin: dict = Depends(require_admin)) -> dict:
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.get("auth_mode") == "ldap":
        raise HTTPException(status_code=409, detail="Пароль этого пользователя хранится в каталоге")
    password = auth.generate_password()
    try:
        updated = store.update_user(user_id, expected_mode="local", password_hash=auth.hash_password(password))
    except store.CredentialsChanged as exc:
        raise HTTPException(409, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "Пользователь не найден")
    store.record_audit(_actor_name(_admin), "user.password", user["email"], "выдан новый локальный пароль")
    return {"password": password}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, _admin: dict = Depends(require_admin)) -> dict:
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        removed = store.delete_user(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Нельзя удалить профиль, пока его встреча обрабатывается",
        ) from exc
    if removed is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    for meeting_id in removed:
        retention.cleanup_recording(meeting_id)
    store.record_audit(
        _actor_name(_admin),
        "user.delete",
        user["email"],
        f"встреч {len(removed)}",
    )
    return {"ok": True}


@app.get("/api/settings")
def get_settings(_admin: dict = Depends(require_admin)) -> dict:
    return _health(full=True)


@app.get("/api/admin/monitoring")
def admin_monitoring(response: Response, _admin: dict = Depends(require_admin)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {
        "queue": store.queue_monitoring(),
        "resources": monitoring.resources.snapshot(),
        "performance": performance.snapshot(),
        "disk": disk.snapshot(),
        "sampled_at": store.utc_now(),
    }


@app.get("/api/admin/diagnostics")
def admin_diagnostics(response: Response, days: int = Query(7, ge=1, le=14),
                      _admin: dict = Depends(require_admin)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return diagnostics.summary(days)


@app.get("/api/admin/diagnostics/export")
def export_diagnostics(days: int = Query(7, ge=1, le=14),
                       _admin: dict = Depends(require_admin)) -> Response:
    report = diagnostics.export_report(days)
    if not report.get("available", False):
        raise HTTPException(503, "Отчёт временно недоступен. Повторите скачивание.",
                            headers={"Cache-Control": "no-store"})
    return Response(
        content=json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")),
        media_type="application/json",
        headers={"Cache-Control": "no-store",
                 "Content-Disposition": f'attachment; filename="mom-performance-{days}d.json"'},
    )


@app.get("/api/settings/prompts")
def get_prompts(_admin: dict = Depends(require_admin)) -> dict:
    return prompts.settings_view()


@app.post("/api/settings/prompts")
def save_prompts(payload: PromptsIn, _admin: dict = Depends(require_admin)) -> dict:
    values = payload.model_dump()
    for key in ("protocol_prompt", "name_review_prompt"):
        values[key] = values[key].strip()
        if not values[key]:
            raise HTTPException(status_code=400, detail="Промпт не может быть пустым")
    version = store.save_llm_settings(values, _actor_name(_admin))
    store.record_audit(_actor_name(_admin), "prompts.save", "llm", f"версия {version}")
    return prompts.settings_view()


async def _verify_until_disconnect(request: Request, key: str | None = None) -> None:
    disconnected = False
    async with anyio.create_task_group() as tasks:
        async def watch_disconnect() -> None:
            nonlocal disconnected
            while (await request.receive())["type"] != "http.disconnect":
                continue
            disconnected = True
            tasks.cancel_scope.cancel()

        tasks.start_soon(watch_disconnect)
        await keys.verify_key(key)
        tasks.cancel_scope.cancel()

    if disconnected:
        raise HTTPException(status_code=499, detail="Проверка отменена")


@app.post("/api/settings")
async def save_settings(
    payload: SettingsIn,
    request: Request,
    verify: bool = True,
    _admin: dict = Depends(require_admin),
) -> dict:
    key = payload.openai_api_key.strip()
    if " " in key or len(key) < 20:
        raise HTTPException(status_code=400, detail="Похоже, это не API-ключ")
    def persist():
        config.set_openai_api_key(key)
        keys.reset_status("Ключ сохранён. Проверьте подключение.")
        store.record_audit(_actor_name(_admin), "key.save", "llm", "ключ сохранён")
    await run_in_threadpool(persist)
    if verify:
        await _verify_until_disconnect(request, key)
    return await run_in_threadpool(_health, full=True)


@app.post("/api/settings/models")
def save_models(payload: ModelsIn, _admin: dict = Depends(require_admin)) -> dict:
    previous = config.get_chat_model()
    chat = (payload.chat_model or previous).strip()
    asr = (payload.asr_model or config.get_asr_model()).strip()
    if not chat or not asr:
        raise HTTPException(status_code=400, detail="Укажите модели чата и расшифровки")
    if payload.asr_model is None:
        config.save_chat_model(chat)
    else:
        if asr == config.REMOTE_ASR and not config.get_asr_connection().model:
            raise HTTPException(400, "Сначала настройте API во вкладке «Расшифровка».")
        config.apply_connection(config.get_base_url(), chat, asr)
    if chat != previous:
        keys.reset_status("Подключение ещё не проверено")
    store.record_audit(_actor_name(_admin), "models.save", "llm", f"чат {chat}, расшифровка {asr}")
    return _health(full=True)


@app.post("/api/settings/asr")
def save_asr_settings(payload: AsrSettingsIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        config.save_asr(payload.engine, payload.base_url, payload.api_key.get_secret_value(), payload.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    store.record_audit(_actor_name(_admin), "asr.save", "asr", "Настройки расшифровки сохранены")
    return _health(full=True)


def _asr_draft(payload: AsrApiIn) -> config.AsrConnection:
    try:
        return config.resolve_asr_connection(payload.base_url, payload.api_key.get_secret_value(), payload.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/settings/asr/models")
async def asr_models(payload: AsrApiIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        async with remote_asr.connect(_asr_draft(payload), timeout=config.VERIFY_TIMEOUT_SECONDS) as service:
            return {"models": await service.models()}
    except remote_asr.AsrServiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/settings/asr/test")
async def test_asr_settings(payload: AsrApiIn, _admin: dict = Depends(require_admin)) -> dict:
    if not payload.model.strip():
        raise HTTPException(400, "Укажите модель расшифровки.")
    try:
        async with remote_asr.connect(_asr_draft(payload), timeout=config.VERIFY_TIMEOUT_SECONDS) as service:
            await service.probe()
    except remote_asr.AsrServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True}


@app.get("/api/settings/chat-models")
async def chat_model_catalog(_admin: dict = Depends(require_admin)) -> dict:
    if not config.get_api_key():
        return {"models": [], "manual": True}
    try:
        models = await keys.list_chat_models()
    except Exception:
        log.exception("Каталог моделей не открылся")
        return {"models": [], "manual": True}
    return {"models": models, "manual": not models}


@app.post("/api/settings/theme")
def save_theme(payload: ThemeIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        config.set_ui_theme(payload.theme)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _health(full=True)


@app.post("/api/settings/smtp")
def save_smtp(payload: SmtpIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        config.set_smtp(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    host = payload.host.strip()
    sender = payload.from_addr.strip()
    store.record_audit(
        _actor_name(_admin),
        "smtp.save",
        "smtp",
        ", ".join(part for part in (host, sender) if part) or "настройки почты",
    )
    return _health(full=True)


@app.post("/api/settings/ldap")
def save_ldap(payload: LdapIn, _admin: dict = Depends(require_admin)) -> dict:
    _check_ldap_settings(payload)
    try:
        config.set_ldap(
            enabled=payload.enabled, url=payload.url, bind_dn=payload.bind_dn,
            bind_password=payload.bind_password, base_dn=payload.base_dn,
            user_filter=payload.user_filter, starttls=payload.starttls,
            tls_verify=payload.tls_verify, email_attr=payload.email_attr,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        log.exception("Не удалось сохранить настройки AD")
        raise HTTPException(status_code=503, detail="Не удалось записать настройки AD. "
                            "Работающее подключение не изменено. Проверьте права на файл настроек и свободное место") from exc
    state = "включён" if payload.enabled else "выключен"
    bits = [state, payload.url.strip()]
    base = payload.base_dn.strip()
    if base:
        bits.append(f"база {base}")
    store.record_audit(_actor_name(_admin), "ldap.save", "ldap", ", ".join(bit for bit in bits if bit))
    return _health(full=True)


@app.get("/api/settings/ldap/scopes")
def get_directory_scopes(_admin: dict = Depends(require_admin)) -> dict:
    return directory_scope.snapshot()


@app.post("/api/settings/ldap/scopes")
def save_directory_scopes(payload: DirectoryScopeIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        result = directory_scope.save(payload.mode, payload.bases)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.record_audit(_actor_name(_admin), "ldap.scopes", "ldap", f"Режим {payload.mode}, областей {len(result['bases'])}")
    return result


@app.get("/api/settings/ldap/structure")
def directory_structure(base_dn: str = Query(default="", max_length=2000),
                        cursor: str = Query(default="", max_length=8192),
                        _admin: dict = Depends(require_admin)) -> dict:
    try:
        return ldap_auth.browse_scopes(base_dn, cursor)
    except ldap_auth.LdapError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/ldap/test")
def test_ldap(payload: LdapTestIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        if payload.settings is None:
            return ldap_auth.test_connection(payload.username)
        _check_ldap_settings(payload.settings.model_copy(update={"enabled": True}))
        options = config.resolve_ldap_settings(**payload.settings.model_dump())
        with config.ldap_settings_scope(options):
            return ldap_auth.test_connection(payload.username, check_base=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ldap_auth.LdapError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/queue")
def save_queue(payload: QueueIn, _admin: dict = Depends(require_admin)) -> dict:
    config.set_max_jobs(payload.max_jobs)
    worker.notify_work()
    return _health(full=True)


@app.post("/api/settings/smtp/test")
def test_smtp(payload: SmtpTestIn, _admin: dict = Depends(require_admin)) -> dict:
    if not config.smtp_configured():
        raise HTTPException(status_code=400, detail="Сначала сохраните SMTP-сервер и адрес отправителя")
    to = (payload.to or config.get_smtp_from() or "").strip().lower()
    if "@" not in to:
        raise HTTPException(status_code=400, detail="Укажите email для проверки")
    try:
        send_mail(to, "MoM: проверка почты", "Если вы видите это письмо, SMTP настроен верно.\n")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SMTP не принял письмо: {exc}") from exc
    return {"ok": True, "to": to}


@app.post("/api/settings/verify")
async def verify_settings(request: Request, _admin: dict = Depends(require_admin)) -> dict:
    if not config.get_api_key():
        raise HTTPException(status_code=400, detail="Сначала сохраните ключ")
    await _verify_until_disconnect(request)
    return await run_in_threadpool(_health, full=True)


@app.delete("/api/settings")
def delete_settings(_admin: dict = Depends(require_admin)) -> dict:
    config.clear_openai_api_key()
    keys.reset_status("Ключ удалён")
    store.record_audit(_actor_name(_admin), "key.delete", "llm", "ключ удалён")
    return _health(full=True)


@app.get("/api/people")
def list_people(q: str = Query(default="", max_length=200), _user: dict = Depends(require_user)) -> dict:
    _search_rate_or_429("user:" + _user["user_id"])
    return people.suggest(q)


@app.get("/api/meetings")
def list_meetings(user: dict = Depends(require_user)) -> list[dict]:
    return store.list_meetings(user["user_id"])


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    return _owned(store.get_meeting(meeting_id), user)


def _disk_or_507(extra: int = 0) -> None:
    try:
        disk.ensure_space(extra)
    except disk.DiskError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@app.post("/api/meetings/transcript")
async def import_transcript(request: Request, user: dict = Depends(require_user)) -> dict:
    # Dependencies authenticate before the first byte is read or file spooled.
    async with upload.bounded_form(request, limit=transcript_import.MAX_REQUEST_BYTES,
                                   message="Транскрипт больше 1 МБ", max_fields=3,
                                   max_part_size=transcript_import.MAX_REQUEST_BYTES, allow_urlencoded=True) as form:
        if any(key not in {"text", "file", "title", "calendar_event_id"} or len(form.getlist(key)) != 1 for key in form):
            raise HTTPException(400, "Нужен один текст или один файл и название")
        text, file, title = form.get("text"), form.get("file"), form.get("title", "")
        if not isinstance(title, str) or len(title) > 200:
            raise HTTPException(400, "Название должно быть не длиннее 200 символов")
        if (text is None) == (file is None):
            raise HTTPException(400, "Передайте либо текст, либо файл")
        calendar_context = await run_in_threadpool(_calendar_selection, request, user, form.get("calendar_event_id"))
        original, suffix = "transcript.txt", ".txt"
        if file is not None:
            if not isinstance(file, StarletteUploadFile):
                raise HTTPException(400, "Нужен текстовый файл")
            original = Path((file.filename or "").replace("\\", "/")).name
            suffix = Path(original).suffix.lower()
            if suffix not in transcript_import.SUFFIXES:
                raise HTTPException(400, "Нужен файл .txt, .md, .srt или .vtt в UTF-8")
            raw = await file.read(transcript_import.MAX_TEXT_BYTES + 1)
            if len(raw) > transcript_import.MAX_TEXT_BYTES:
                raise HTTPException(413, "Транскрипт больше 1 МБ")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise HTTPException(400, "Сохраните файл в кодировке UTF-8") from exc
        if not isinstance(text, str):
            raise HTTPException(400, "Нужен текст транскрипта")
        if len(text.encode("utf-8")) > transcript_import.MAX_TEXT_BYTES:
            raise HTTPException(413, "Транскрипт больше 1 МБ")
        try:
            document = await run_in_threadpool(transcript_import.parse_transcript, text, suffix)
        except transcript_import.TranscriptError as exc:
            raise HTTPException(400, str(exc)) from exc
    await run_in_threadpool(_disk_or_507, len(text.encode("utf-8")) * 4)
    try:
        meeting = await run_in_threadpool(store.create_text_meeting,
            uuid.uuid4().hex, title.strip() or (calendar_context or {}).get("title") or (Path(original).stem[:200] if file else "Текстовая встреча"),
            original[:255], user["user_id"], document, calendar_context=calendar_context,
        )
    except store.MeetingOwnerMissing as exc:
        raise HTTPException(401, "Профиль удалён во время загрузки. Войдите заново.") from exc
    return meeting


@app.post("/api/meetings")
async def create_meeting(
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    # Dependencies run before any multipart parsing or temporary file creation.
    _require_model_ready(protocol=False)
    received_at = time.perf_counter()
    async with upload.audio_form(request, _disk_or_507) as form:
        received_seconds = time.perf_counter() - received_at
        if any(key not in {"file", "title", "calendar_event_id"} or len(form.getlist(key)) != 1 for key in form):
            raise HTTPException(400, "Некорректные поля загрузки записи")
        file, title = form.get("file"), form.get("title")
        if not isinstance(file, StarletteUploadFile) or (title is not None and not isinstance(title, str)):
            raise HTTPException(status_code=422, detail="Нужен файл записи и текстовое название")
        calendar_context = await run_in_threadpool(_calendar_selection, request, user, form.get("calendar_event_id"))
        save_started = time.perf_counter()
        meeting = await upload.run_file_operation(_create_audio_meeting, request, file, title, user,
                                                  calendar_context=calendar_context)
        try:
            await run_in_threadpool(diagnostics.record_upload, meeting["id"],
                                   receive_seconds=received_seconds,
                                   save_seconds=time.perf_counter() - save_started,
                                   size_bytes=file.size)
        except Exception:
            log.warning("Performance upload diagnostics unavailable")
        return meeting


def _calendar_selection(request: Request, user: dict, event_id) -> dict | None:
    try:
        return calendar_service.selected_context(request.cookies.get(auth.USER_COOKIE), user["user_id"], event_id)
    except ExchangeError as exc:
        raise HTTPException(exc.status,str(exc)) from exc


def _require_model_ready(*, protocol: bool) -> None:
    status = keys.snapshot()
    if protocol and not status["connected"]:
        raise HTTPException(400, status.get("message") or "Проверьте подключение модели протокола.")
    if not protocol and not status.get("asr_ready", status["connected"]):
        raise HTTPException(400, "Проверьте подключение модели распознавания в разделе «Расшифровка».")


def _create_audio_meeting(request: Request, file: StarletteUploadFile, title: str | None, user: dict,
                                *, calendar_context: dict | None = None) -> dict:
    if not ffmpeg_available():
        raise HTTPException(status_code=500, detail="Установите ffmpeg и ffprobe")

    original = file.filename or "recording.webm"
    suffix = Path(original).suffix.lower() or ".webm"
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="Нужен файл записи: webm, mkv, mp4, mp3, wav, m4a или ogg",
        )

    meeting_id = uuid.uuid4().hex
    with retention.incoming_recording(meeting_id):
        staged = UPLOAD_DIR / f"{meeting_id}.uploading"
        size = 0
        limit = config.MAX_UPLOAD_BYTES
        declared = 0
        raw_length = request.headers.get("content-length") or ""
        if raw_length.isdigit():
            declared = int(raw_length)
        _disk_or_507(declared)
        try:
            with staged.open("xb") as out:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Файл больше {limit // (1024 * 1024)} МБ",
                        )
                    if size % (8 * 1024 * 1024) < len(chunk):
                        _disk_or_507()
                    out.write(chunk)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise HTTPException(status_code=507, detail="Диск заполнился во время загрузки") from exc
            raise
        if size == 0:
            raise HTTPException(status_code=400, detail="Пустой файл")
        if not looks_like_audio(staged):
            raise HTTPException(
                status_code=400,
                detail="Файл не похож на запись (webm, mkv, mp4, mp3, wav, m4a или ogg)",
            )

        staged.replace(UPLOAD_DIR / f"{meeting_id}{suffix}")
        display_title = (title or "").strip() or (calendar_context or {}).get("title") or Path(original).stem
        try:
            meeting = store.create_meeting(meeting_id, display_title, original, user["user_id"], calendar_context=calendar_context)
        except store.MeetingOwnerMissing as exc:
            raise HTTPException(401, "Профиль удалён во время загрузки. Войдите заново.") from exc
        worker.notify_work()
        return meeting


@app.post("/api/meetings/{meeting_id}/retry")
def retry_meeting(
    meeting_id: str,
    user: dict = Depends(require_user),
    revision: str | None = None,
) -> dict:
    # The worker claims jobs in its own transaction. Keep the eligibility check
    # and enqueue together so concurrent retries cannot requeue an active job.
    with store.edit_meeting(meeting_id) as (connection, row):
        meeting = _owned(row, user)
        if meeting["status"] not in {"error", "done"}:
            raise HTTPException(status_code=409, detail="Эта встреча ещё обрабатывается")
        if store.is_manual_protocol(meeting.get("result")) or revision is not None:
            if revision != meeting["result_revision"]:
                raise HTTPException(409, "Откройте актуальную версию протокола и подтвердите замену ручных правок.")
            if not (meeting.get("transcript") or "").strip():
                raise HTTPException(409, "Для пересборки протокола нужна сохранённая расшифровка.")
        resume = store.is_manual_protocol(meeting.get("result")) or meeting.get("source_type") == "text" or meeting["status"] == "error"
        _require_model_ready(protocol=bool(resume and meeting.get("transcript")))
        _check_mail_queue(connection, meeting_id)
        store.queue_reprocessing(meeting, connection=connection,
                                 resume_transcript=meeting["status"] == "error")
    store.refresh_queue_messages()
    worker.notify_work()
    updated = store.get_meeting(meeting_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    return updated


def _check_mail_queue(connection: sqlite3.Connection, meeting_id: str) -> None:
    if connection.execute("SELECT 1 FROM protocol_mail WHERE meeting_id=? AND state IN ('pending','sending') LIMIT 1",
                          (meeting_id,)).fetchone():
        raise HTTPException(409, "Предыдущее письмо ещё в очереди. Дождитесь результата отправки.")


def _editable_protocol(meeting: dict, revision: str) -> None:
    if meeting["status"] != "done" or not isinstance(meeting.get("result"), dict):
        raise HTTPException(409, "Дождитесь готовности протокола.")
    if meeting["result_revision"] != revision:
        raise HTTPException(409, "Протокол уже изменился. Проверьте актуальную версию встречи.")


@app.patch("/api/meetings/{meeting_id}/protocol")
def save_protocol(meeting_id: str, payload: protocol_editor.EditRequest | protocol_editor.SummaryEditRequest,
                  user: dict = Depends(require_user)) -> dict:
    with store.edit_meeting(meeting_id) as (connection, row):
        meeting = _owned(row, user)
        _editable_protocol(meeting, payload.revision)
        if isinstance(payload, protocol_editor.SummaryEditRequest):
            result = {**meeting["result"], "summary": payload.summary, "edited_manually": True}
            if payload.summary_format is not None:
                result["summary_format"] = payload.summary_format
            store.update_meeting(meeting_id, connection=connection, result=result)
        else:
            result = protocol_editor.edited_result(meeting["result"], payload.result)
            store.update_meeting(meeting_id, connection=connection, result=result, title=result["title"])
    store.record_audit(_actor_name(user), "protocol.edit", meeting_id)
    return _owned(store.get_meeting(meeting_id), user)


@app.post("/api/meetings/{meeting_id}/protocol/proofread")
async def proofread_protocol(meeting_id: str, payload: protocol_editor.EditRequest | protocol_editor.SummaryEditRequest,
                             request: Request,
                             user: dict = Depends(require_user)) -> dict:
    meeting = _owned(await run_in_threadpool(store.get_meeting, meeting_id), user)
    _editable_protocol(meeting, payload.revision)
    with protocol_editor.proofread_slot(user["user_id"]):
        fields = ({"summary": payload.summary} if isinstance(payload, protocol_editor.SummaryEditRequest)
                  else protocol_editor.prose_fields(payload.result))
        return {"suggestions": await protocol_editor.proofread(fields, request.is_disconnected)}


@app.post("/api/meetings/{meeting_id}/protocol")
def start_protocol(meeting_id: str, payload: ProtocolIn | None = None, user: dict = Depends(require_user)) -> dict:
    with store.edit_meeting(meeting_id) as (connection, row):
        meeting = _owned(row, user)
        if meeting["status"] != "review":
            raise HTTPException(status_code=409, detail="Протокол собирается после готовой расшифровки")
        if not (meeting.get("transcript") or "").strip():
            raise HTTPException(status_code=409, detail="Нет расшифровки")
        _require_model_ready(protocol=True)
        if payload and payload.recipients:
            raise HTTPException(400, "Выберите получателей в готовом протоколе и подтвердите отправку.")
        if not store.queue_review_protocol(meeting_id, connection=connection):
            raise HTTPException(status_code=409, detail="Протокол уже поставлен в очередь")
    store.refresh_queue_messages()
    worker.notify_work()
    return store.get_meeting(meeting_id)


def _halt_processing(meeting_id: str, user: dict, *, restart: bool) -> dict:
    from . import control

    with control.command_lock:
        with store.edit_meeting(meeting_id) as (connection, row):
            meeting = _owned(row, user)
            if meeting["status"] not in store.ACTIVE_STATUSES:
                raise HTTPException(status_code=409, detail="Эта встреча сейчас не обрабатывается")
            if restart:
                _require_model_ready(protocol=bool(meeting.get("transcript") and
                    (store.is_manual_protocol(meeting.get("result")) or meeting.get("source_type") == "text")))
            if meeting["status"] == "queued":
                # No worker can claim this row until the transition commits. Applying
                # queued requests here avoids cancellation flags leaking into a new job.
                store.apply_processing_request(connection, meeting, "restart" if restart else "stop")
            else:
                # A claimed job may not yet have reached control.bind_job. Its worker
                # must acknowledge the stop; finishing here could requeue an owned job.
                connection.execute("INSERT OR REPLACE INTO processing_requests(meeting_id, action, attempt) VALUES (?,?,?)",
                                   (meeting_id, "restart" if restart else "stop", meeting["processing_attempt"]))
                store.update_meeting(
                    meeting_id, connection=connection,
                    status_message="Останавливаю, чтобы запустить заново…" if restart else "Останавливаю…",
                )
        if meeting["status"] != "queued":
            control.request_stop(meeting_id, restart=restart)
    store.refresh_queue_messages()
    worker.notify_work()
    updated = store.get_meeting(meeting_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    return updated


@app.post("/api/meetings/{meeting_id}/stop")
def stop_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    updated = _halt_processing(meeting_id, user, restart=False)
    store.record_audit(_actor_name(user), "meeting.stop", meeting_id, updated.get("title") or "")
    return updated


@app.post("/api/meetings/{meeting_id}/restart")
def restart_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    updated = _halt_processing(meeting_id, user, restart=True)
    store.record_audit(_actor_name(user), "meeting.restart", meeting_id, updated.get("title") or "")
    return updated


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    # Recheck status under the same write lock used to remove the row.
    with store.edit_meeting(meeting_id) as (connection, row):
        meeting = _owned(row, user)
        if meeting["status"] in store.ACTIVE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Нельзя удалить встречу, пока она в очереди или в обработке",
            )
        _check_mail_queue(connection, meeting_id)
        if not store.delete_meeting(meeting_id, connection=connection):
            raise HTTPException(status_code=404, detail="Встреча не найдена")
    retention.cleanup_recording(meeting_id)
    store.record_audit(_actor_name(user), "meeting.delete", meeting_id, meeting.get("title") or "")
    return {"ok": True}


def _segments_doc(meeting: dict) -> dict:
    if not meeting.get("segments"):
        raise HTTPException(status_code=409, detail="В этой записи нет разметки спикеров. Повторите обработку.")
    if meeting["status"] in store.ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="Подождите, пока встреча обрабатывается")
    return {
        "speakers": meeting.get("speakers") or [],
        "segments": meeting.get("segments") or [],
        "note": meeting.get("diarization_note"),
        "name_review": meeting.get("name_review"),
        "corporate_context": meeting.get("corporate_context"),
        "source_type": meeting.get("source_type", "audio"),
    }


def _save_segments(
    connection: sqlite3.Connection, meeting_id: str, document: dict, result: dict | None,
    *, replace_from: str | None = None, replace_to: str | None = None, rebuild: bool = False,
) -> None:
    if document.get("name_review") and (not replace_from or replace_from != replace_to):
        document["name_review"] = {**document["name_review"], "status": "stale",
            "message": "Разметка спикеров изменена. Для новой проверки имён пересоберите протокол."}
    fields = {"transcript": render_transcript(document), "transcript_segments": document}
    if result is not None and not store.is_manual_protocol(result) and replace_from and replace_to:
        fields["result"] = replace_speaker_label(result, replace_from, replace_to)
    if rebuild:
        _check_mail_queue(connection, meeting_id)
        fields["result"] = None
        fields.update(status="queued", status_message="Обновляю протокол после правки спикеров",
                      error=None, progress=88, queued_at=store.utc_now())
    store.update_meeting(meeting_id, connection=connection, **fields)


@app.patch("/api/meetings/{meeting_id}/speakers/{speaker_id}")
def rename_meeting_speaker(
    meeting_id: str,
    speaker_id: str,
    payload: SpeakerNameIn,
    user: dict = Depends(require_user),
) -> dict:
    with store.edit_meeting(meeting_id) as (connection, row):
        meeting = _owned(row, user)
        document = _segments_doc(meeting)
        try:
            updated, old_name, new_name = rename_speaker(
                document, speaker_id, payload.name, payload.email, job_title=payload.job_title,
            )
        except SpeakerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _save_segments(
            connection,
            meeting_id,
            updated,
            meeting.get("result"),
            replace_from=old_name,
            replace_to=new_name,
        )
    if old_name != new_name:
        store.record_audit(_actor_name(user), "speaker.rename", meeting_id, f"{old_name} → {new_name}")
    return store.get_meeting(meeting_id)


@app.post("/api/meetings/{meeting_id}/speakers/{speaker_id}/merge")
def merge_meeting_speakers(meeting_id: str, speaker_id: str, payload: MergeSpeakersIn,
                           user: dict = Depends(require_user)) -> dict:
    with store.edit_meeting(meeting_id) as (connection, row):
        meeting = _owned(row, user)
        document = _segments_doc(meeting)
        try:
            updated = merge_speakers(document, speaker_id, payload.target_id)
        except SpeakerError as exc:
            raise HTTPException(400, str(exc)) from exc
        rebuild = meeting["status"] != "review" and not store.is_manual_protocol(meeting.get("result"))
        _save_segments(connection, meeting_id, updated, None, rebuild=rebuild)
    if rebuild:
        store.refresh_queue_messages()
        worker.notify_work()
    store.record_audit(_actor_name(user), "speaker.merge", meeting_id, f"{speaker_id} → {payload.target_id}")
    return store.get_meeting(meeting_id)


@app.patch("/api/meetings/{meeting_id}/segments")
def reassign_meeting_segments(
    meeting_id: str,
    payload: SpeakerAssignIn,
    user: dict = Depends(require_user),
) -> dict:
    with store.edit_meeting(meeting_id) as (connection, row):
        meeting = _owned(row, user)
        document = _segments_doc(meeting)
        try:
            updated = reassign_segments(document, payload.segment_indexes, payload.speaker_id)
        except SpeakerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        old_ids = {item["id"] for item in document.get("speakers") or []}
        if payload.speaker_id == "new":
            created = next((item for item in updated.get("speakers") or [] if item.get("id") not in old_ids), None)
            label = (created or {}).get("name") or "новый спикер"
        else:
            label = next(
                (item.get("name") for item in updated.get("speakers") or [] if item.get("id") == payload.speaker_id),
                payload.speaker_id,
            )
        rebuild = meeting["status"] != "review" and not store.is_manual_protocol(meeting.get("result"))
        _save_segments(connection, meeting_id, updated, None, rebuild=rebuild)
    store.record_audit(
        _actor_name(user),
        "speaker.reassign",
        meeting_id,
        f"{len(payload.segment_indexes)} реплик → {label}",
    )
    if rebuild:
        store.refresh_queue_messages()
        worker.notify_work()
    return store.get_meeting(meeting_id)


@app.get("/api/meetings/{meeting_id}/turn-audio")
def meeting_turn_audio(meeting_id: str, start_ms: int = Query(ge=0, le=2**53 - 1),
                       end_ms: int = Query(gt=0, le=2**53 - 1),
                       user: dict = Depends(require_user)) -> Response:
    meeting = _owned(store.get_recording_info(meeting_id), user)
    if meeting["status"] not in {"review", "done"}:
        raise HTTPException(409, "Расшифровка ещё не готова")
    if not 0 < end_ms - start_ms <= 60_000:
        raise HTTPException(422, "Укажите фрагмент длительностью до минуты")
    if (Path(meeting["filename"]).suffix.lower() or ".webm") not in ALLOWED_SUFFIXES:
        raise HTTPException(404, "У этой встречи нет аудиозаписи")
    source = audio_source(meeting["id"], meeting["filename"])
    if source is None:
        raise HTTPException(404, "Исходная запись недоступна")
    try:
        clip = playback_clip(source, start_ms / 1000, (end_ms - start_ms) / 1000)
    except AudioError as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(clip, media_type="audio/mpeg", headers={"Cache-Control": "private, no-store"})


@app.get("/api/meetings/{meeting_id}/export.md", response_class=PlainTextResponse)
def export_markdown(meeting_id: str, user: dict = Depends(require_user)) -> str:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    return to_markdown(meeting)


@app.get("/api/meetings/{meeting_id}/export.docx", response_class=Response)
def export_word(meeting_id: str, user: dict = Depends(require_user)) -> Response:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    try:
        result = StoredProtocolResult.model_validate(meeting["result"]).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail="Не удалось прочитать сохранённый протокол") from exc
    filename = quote(word_protocol.docx_filename(result["title"] or meeting["title"]), safe="")
    return Response(word_protocol.to_docx(meeting, result), media_type=word_protocol.MEDIA_TYPE, headers={
        "Content-Disposition": f"attachment; filename=protocol.docx; filename*=UTF-8''{filename}",
        "Cache-Control": "no-store",
    })


@app.post("/api/meetings/{meeting_id}/protocol/send")
def confirm_protocol_mail(meeting_id: str, payload: ProtocolSendIn, user: dict = Depends(require_user)) -> dict:
    meeting = _owned(store.get_meeting(meeting_id), user)
    try:
        addresses = list(dict.fromkeys(clean_speaker_email(value) for value in payload.recipients))
    except SpeakerError as exc:
        raise HTTPException(400, str(exc)) from exc
    recipients_hash = hashlib.sha256("\n".join(addresses).encode()).hexdigest()

    def was_confirmed(conn: sqlite3.Connection) -> bool:
        previous = conn.execute(
            "SELECT revision, recipients_hash FROM protocol_send_requests WHERE meeting_id=? AND request_id=?",
            (meeting_id, payload.request_id),
        ).fetchone()
        if previous and tuple(previous) != (payload.revision, recipients_hash):
            raise HTTPException(409, "Список адресов или версия изменились. Подтвердите отправку заново.")
        return previous is not None

    with store.connect() as conn:
        if was_confirmed(conn):
            return meeting
    _editable_protocol(meeting, payload.revision)
    if not config.smtp_configured():
        raise HTTPException(400, "Настройте SMTP для отправки протокола")
    try:
        packet = prepare_protocol_mail(meeting)
    except ValidationError as exc:
        raise HTTPException(409, "Не удалось прочитать сохранённый протокол") from exc
    # Render outside the writer lock, then atomically verify the version and publish one envelope.
    with store.edit_meeting(meeting_id) as (conn, row):
        current = _owned(row, user)
        replay = was_confirmed(conn)
        if not replay:
            _editable_protocol(current, payload.revision)
            _check_mail_queue(conn, meeting_id)
            if current["speakers"] != meeting["speakers"]:
                raise HTTPException(409, "Разметка изменилась. Проверьте участников и повторите отправку.")
            queue_protocol_mail(conn, meeting, packet, addresses)
            conn.execute(
                "INSERT INTO protocol_send_requests (meeting_id, request_id, revision, recipients_hash) VALUES (?, ?, ?, ?)",
                (meeting_id, payload.request_id, payload.revision, recipients_hash),
            )
    if not replay:
        store.record_audit(_actor_name(user), "email.confirm", meeting_id, f"Получателей: {len(addresses)}")
    return _owned(store.get_meeting(meeting_id), user)


@app.get("/api/meetings/{meeting_id}/email.txt", response_class=PlainTextResponse)
def meeting_email_text(meeting_id: str, user: dict = Depends(require_user)) -> str:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    return email_body(meeting)


@app.get("/api/meetings/{meeting_id}/email.eml", response_class=Response)
def download_meeting_email(meeting_id: str, user: dict = Depends(require_user)) -> Response:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    try:
        with store.connect() as conn:
            recipients = [r[0] for r in conn.execute("""
                SELECT recipient FROM protocol_mail WHERE meeting_id=? AND ?=(
                    SELECT revision FROM protocol_send_requests WHERE meeting_id=? ORDER BY rowid DESC LIMIT 1
                ) ORDER BY id""", (meeting_id, meeting["result_revision"], meeting_id))]
        draft = build_eml(meeting, recipients)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail="Не удалось прочитать сохранённый протокол") from exc
    store.record_audit(_actor_name(user), "email.open", meeting_id, "скачан черновик с протоколом")
    return Response(draft, media_type="message/rfc822", headers={
        "Content-Disposition": 'attachment; filename="protocol.eml"', "Cache-Control": "no-store",
    })


@app.post("/api/meetings/{meeting_id}/email", deprecated=True)
def send_meeting_email(meeting_id: str, user: dict = Depends(require_user)) -> Response:
    # Legacy clients get the same download; never launch programs or persist drafts.
    return download_meeting_email(meeting_id, user)


def to_markdown(meeting: dict) -> str:
    result = meeting["result"]
    duration = format_duration(meeting.get("duration_seconds"))
    lines = [
        f"# {result.get('title') or meeting['title']}",
        "",
        f"- Дата обработки: {meeting['created_at']}",
    ]
    if duration:
        lines.append(f"- Длительность: {duration}")
    if result.get("date_hint"):
        lines.append(f"- Дата из обсуждения: {result['date_hint']}")
    if participants := participant_labels(meeting, result):
        lines.append("- Участники: " + ", ".join(participants))
    lines += ["", "## Саммари", "", result.get("summary") or "—", ""]

    if result.get("key_points"):
        lines += ["## Ключевые тезисы", ""]
        lines += [f"- {item}" for item in result["key_points"]]
        lines.append("")

    if result.get("decisions"):
        lines += ["## Решения", ""]
        lines += [f"- {item}" for item in result["decisions"]]
        lines.append("")

    lines += ["## Поручения", ""]
    items = result.get("action_items") or []
    if not items:
        lines.append("Поручений не зафиксировано.")
    else:
        for item in items:
            who = item.get("assignee") or "не назначен"
            due = item.get("due") or "без срока"
            lines.append(
                f"- **{item['task']}** — {who}; срок: {due}; приоритет: {item.get('priority')}"
            )
    lines.append("")

    mom = result.get("mom") or {}
    lines += ["## Протокол (MoM)", ""]
    for block in mom.get("agenda") or []:
        lines.append(f"### {block.get('topic') or 'Тема'}")
        if block.get("discussion"):
            lines.append(block["discussion"])
        if block.get("outcome"):
            lines.append(f"**Итог:** {block['outcome']}")
        lines.append("")
    if mom.get("next_meeting"):
        lines += ["**Следующая встреча:** " + mom["next_meeting"], ""]

    if meeting.get("transcript"):
        lines += ["## Транскрипт", "", "```", meeting["transcript"], "```", ""]
    return "\n".join(lines).strip() + "\n"


@app.get("/", include_in_schema=False)
def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(
            index_file,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )
    return HTMLResponse(
        "<h1>MoM</h1><p>Соберите интерфейс: <code>./install.sh</code> или <code>cd frontend && npm run build</code></p>",
        status_code=503,
    )


@app.get("/admin", include_in_schema=False)
@app.get("/admin/{rest:path}", include_in_schema=False)
def admin_index(rest: str = ""):
    return index()


if (STATIC_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
