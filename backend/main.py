from __future__ import annotations

import asyncio
import errno
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, disk, keys, ldap_auth, store, worker
from .audio import ffmpeg_available, looks_like_audio
from .config import AUDIO_DIR, UPLOAD_DIR, ensure_dirs
from .mail import email_body, open_or_save_eml, send_mail, smtp_snapshot
from .pipeline import format_duration
from .speakers import SpeakerError, reassign_segments, rename_speaker, render_transcript, replace_speaker_label

ALLOWED_SUFFIXES = {".webm", ".mp4", ".mp3", ".wav", ".m4a", ".ogg"}
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


class SettingsIn(BaseModel):
    openai_api_key: str = Field(min_length=8)


class ModelsIn(BaseModel):
    chat_model: str | None = None
    asr_model: str | None = None


class ThemeIn(BaseModel):
    theme: str


class SmtpIn(BaseModel):
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    user: str = ""
    password: str = ""
    from_addr: str = ""
    starttls: bool = True
    public_url: str = ""


class QueueIn(BaseModel):
    max_jobs: int = Field(ge=1, le=8)


class LdapIn(BaseModel):
    enabled: bool = False
    url: str = ""
    bind_dn: str = ""
    bind_password: str = ""
    base_dn: str = ""
    user_filter: str = "(mail={username})"
    starttls: bool = False
    tls_verify: bool = True
    email_attr: str = "mail"


class LdapTestIn(BaseModel):
    username: str = ""


class SpeakerNameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class SpeakerAssignIn(BaseModel):
    speaker_id: str = Field(min_length=1, max_length=40)
    segment_indexes: list[int] = Field(min_length=1, max_length=500)


class SmtpTestIn(BaseModel):
    to: str = ""


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    setup_token: str = ""


class UserLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    archive_limit: int = Field(default=5, ge=1, le=100)
    auth_mode: str = "local"


class UserPatchIn(BaseModel):
    archive_limit: int | None = Field(default=None, ge=1, le=100)
    auth_mode: str | None = None


class UserLimitIn(BaseModel):
    archive_limit: int = Field(ge=1, le=100)


def _peer_trusted(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in _LOOPBACK


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
        raise HTTPException(status_code=429, detail="Слишком много попыток, подождите минуту")


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


def _purge_files(meeting_id: str) -> None:
    for leftover in UPLOAD_DIR.glob(f"{meeting_id}.*"):
        leftover.unlink(missing_ok=True)
    (AUDIO_DIR / f"{meeting_id}.mp3").unlink(missing_ok=True)
    chunks = AUDIO_DIR / f"{meeting_id}_chunks"
    if chunks.exists():
        shutil.rmtree(chunks, ignore_errors=True)


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


def _note_purged(actor: str, removed: list[dict]) -> None:
    for item in removed:
        meeting_id = item.get("id") or ""
        if not meeting_id:
            continue
        store.record_audit(
            actor,
            "meeting.delete",
            meeting_id,
            f"{item.get('title') or 'встреча'} · вытеснено из архива",
        )
        _purge_files(meeting_id)


_EMAIL_OPENED = {
    "saved": "черновик сохранён",
    "opened-outlook": "открыт в Outlook",
    "opened-mail": "открыт в Почте",
    "opened": "открыт файл письма",
}


def _normalize_auth_mode(value: str) -> str:
    mode = (value or "local").strip().lower()
    if mode not in {"local", "ldap"}:
        raise HTTPException(status_code=400, detail="Режим входа: local или ldap")
    if mode == "ldap" and not config.ldap_configured():
        raise HTTPException(status_code=400, detail="Сначала сохраните и включите LDAP в админке")
    return mode


def _health(*, full: bool = False) -> dict:
    disk_status = disk.snapshot()
    if not full:
        disk_status = {"ok": disk_status["ok"]}
    return {
        "ok": True,
        "ffmpeg": ffmpeg_available(),
        "ui": (STATIC_DIR / "index.html").exists(),
        "theme": config.get_ui_theme(),
        "openai": keys.snapshot() if full else keys.public_snapshot(),
        "queue": store.queue_stats(),
        "smtp": smtp_snapshot(full=full),
        "ldap": ldap_snapshot(full=full),
        "disk": disk_status,
    }

@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    store.init_db()
    store.requeue_interrupted_meetings()
    config.migrate_token_plan_asr()
    if config.get_api_key():
        try:
            await asyncio.wait_for(keys.verify_key(thorough=False), timeout=float(config.VERIFY_TIMEOUT_SECONDS) + 5)
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
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.get("/api/health")
def health() -> dict:
    return _health(full=False)


@app.get("/api/auth/status")
def auth_status(mom_admin: str | None = Cookie(default=None)) -> dict:
    return auth.status(mom_admin)


@app.post("/api/auth/setup")
def auth_setup(payload: LoginIn, request: Request, response: Response) -> dict:
    _rate_or_429(request, "setup")
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
    _rate_or_429(request, "admin")
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
    _rate_or_429(request, "user")
    email = _normalize_email(payload.email)
    try:
        record = auth.authenticate_platform_user(email, payload.password)
    except ldap_auth.LdapError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not record:
        store.record_audit(email or "пользователь", "user.login.fail", email, "неверный email или пароль")
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    token = auth.create_user_session(record["id"], record["email"])
    store.record_audit(record["email"], "user.login", record["email"], "вход в профиль")
    response.set_cookie(auth.USER_COOKIE, token, **_cookie_kw(request))
    user = store.get_user(record["id"])
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
    auth.drop_user_session(mom_user)
    _clear_cookie(response, auth.USER_COOKIE, request)
    return {"authenticated": False, "email": None, "archive_limit": None}


@app.get("/api/admin/audit")
def admin_audit(limit: int = 200, _admin: dict = Depends(require_admin)) -> list[dict]:
    return store.list_audit(limit)


@app.get("/api/admin/users")
def admin_list_users(_admin: dict = Depends(require_admin)) -> list[dict]:
    return store.list_users()


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
        if payload.archive_limit != current.get("archive_limit"):
            store.record_audit(
                actor,
                "user.limit",
                current["email"],
                f"архив {current.get('archive_limit')} → {payload.archive_limit}",
            )
    if payload.auth_mode is not None:
        mode = _normalize_auth_mode(payload.auth_mode)
        fields["auth_mode"] = mode
        if mode == "ldap":
            fields["password_hash"] = auth.ldap_password_placeholder()
        elif current.get("auth_mode") != "local":
            generated = auth.generate_password()
            fields["password_hash"] = auth.hash_password(generated)
        if mode != current.get("auth_mode"):
            auth.drop_user_sessions_for(user_id)
            store.record_audit(
                actor,
                "user.auth",
                current["email"],
                f"{current.get('auth_mode') or 'local'} → {mode}",
            )
    if fields:
        store.update_user(user_id, **fields)
    if payload.archive_limit is not None:
        _note_purged(actor, store.prune_user_meetings(user_id))
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
    store.update_user(user_id, password_hash=auth.hash_password(password))
    auth.drop_user_sessions_for(user_id)
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
        _purge_files(meeting_id)
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


@app.post("/api/settings")
async def save_settings(payload: SettingsIn, _admin: dict = Depends(require_admin)) -> dict:
    key = payload.openai_api_key.strip()
    if " " in key or len(key) < 20:
        raise HTTPException(status_code=400, detail="Похоже, это не API-ключ")
    config.set_openai_api_key(key)
    store.record_audit(_actor_name(_admin), "key.save", "llm", "ключ сохранён")
    await keys.verify_key(key)
    return _health(full=True)


@app.post("/api/settings/models")
def save_models(payload: ModelsIn, _admin: dict = Depends(require_admin)) -> dict:
    chat = (payload.chat_model or config.get_chat_model()).strip()
    asr = (payload.asr_model or config.get_asr_model()).strip()
    if not chat or not asr:
        raise HTTPException(status_code=400, detail="Укажите модели чата и расшифровки")
    config.apply_connection(config.get_base_url(), chat, asr)
    store.record_audit(_actor_name(_admin), "models.save", "llm", f"чат {chat}, расшифровка {asr}")
    return _health(full=True)


@app.post("/api/settings/theme")
def save_theme(payload: ThemeIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        config.set_ui_theme(payload.theme)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _health(full=True)


@app.post("/api/settings/smtp")
def save_smtp(payload: SmtpIn, _admin: dict = Depends(require_admin)) -> dict:
    config.set_smtp(
        host=payload.host,
        port=payload.port,
        user=payload.user,
        password=payload.password,
        from_addr=payload.from_addr,
        starttls=payload.starttls,
    )
    config.set_public_base_url(payload.public_url)
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
    config.set_ldap(
        enabled=payload.enabled,
        url=payload.url,
        bind_dn=payload.bind_dn,
        bind_password=payload.bind_password,
        base_dn=payload.base_dn,
        user_filter=payload.user_filter,
        starttls=payload.starttls,
        tls_verify=payload.tls_verify,
        email_attr=payload.email_attr,
    )
    state = "включён" if payload.enabled else "выключен"
    bits = [state, payload.url.strip()]
    base = payload.base_dn.strip()
    if base:
        bits.append(f"база {base}")
    store.record_audit(_actor_name(_admin), "ldap.save", "ldap", ", ".join(bit for bit in bits if bit))
    return _health(full=True)


@app.post("/api/settings/ldap/test")
def test_ldap(payload: LdapTestIn, _admin: dict = Depends(require_admin)) -> dict:
    try:
        return ldap_auth.test_connection(payload.username)
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
async def verify_settings(_admin: dict = Depends(require_admin)) -> dict:
    if not config.get_api_key():
        raise HTTPException(status_code=400, detail="Сначала сохраните ключ")
    await keys.verify_key()
    return _health(full=True)


@app.delete("/api/settings")
def delete_settings(_admin: dict = Depends(require_admin)) -> dict:
    config.clear_openai_api_key()
    keys.reset_status("Ключ удалён")
    store.record_audit(_actor_name(_admin), "key.delete", "llm", "ключ удалён")
    return _health(full=True)


@app.get("/api/meetings")
def list_meetings(user: dict = Depends(require_user)) -> list[dict]:
    return store.list_meetings(user["user_id"])


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    return _owned(store.get_meeting(meeting_id), user)


@app.post("/api/meetings")
def _disk_or_507(extra: int = 0) -> None:
    try:
        disk.ensure_space(extra)
    except disk.DiskError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


async def create_meeting(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    user: dict = Depends(require_user),
) -> dict:
    if not ffmpeg_available():
        raise HTTPException(status_code=500, detail="Установите ffmpeg и ffprobe")
    if not keys.snapshot()["connected"]:
        status = keys.snapshot()
        raise HTTPException(
            status_code=400,
            detail=status["message"] or "Сначала подключите ключ в разделе «Администрирование»",
        )

    original = file.filename or "recording.webm"
    suffix = Path(original).suffix.lower() or ".webm"
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="Нужен файл записи: webm, mp4, mp3, wav, m4a или ogg",
        )

    meeting_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{meeting_id}{suffix}"
    size = 0
    limit = config.MAX_UPLOAD_BYTES
    declared = 0
    raw_length = request.headers.get("content-length") or ""
    if raw_length.isdigit():
        declared = int(raw_length)
    _disk_or_507(declared)
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
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
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            raise HTTPException(status_code=507, detail="Диск заполнился во время загрузки") from exc
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Пустой файл")
    if not looks_like_audio(dest):
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="Файл не похож на запись (webm, mp4, mp3, wav, m4a или ogg)",
        )

    display_title = (title or "").strip() or Path(original).stem
    meeting = store.create_meeting(meeting_id, display_title, original, user["user_id"])
    _note_purged(_actor_name(user), store.prune_user_meetings(user["user_id"]))
    worker.notify_work()
    return meeting


@app.post("/api/meetings/{meeting_id}/retry")
async def retry_meeting(
    meeting_id: str,
    user: dict = Depends(require_user),
) -> dict:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] not in {"error", "done"}:
        raise HTTPException(status_code=409, detail="Эта встреча ещё обрабатывается")
    if not keys.snapshot()["connected"]:
        status = keys.snapshot()
        raise HTTPException(
            status_code=400,
            detail=status["message"] or "Сначала подключите ключ в разделе «Администрирование»",
        )
    keep_transcript = meeting["status"] == "error" and (meeting.get("transcript") or "").strip()
    fields: dict = {
        "status": "queued",
        "status_message": "Продолжаю с готовой расшифровки" if keep_transcript else "Повторная обработка в очереди",
        "error": None,
        "result": None,
        "progress": 88 if keep_transcript else 0,
        "queued_at": store.utc_now(),
    }
    if not keep_transcript:
        fields["transcript"] = None
        fields["transcript_segments"] = None
        fields["language"] = None
    store.update_meeting(meeting_id, **fields)
    store.refresh_queue_messages()
    worker.notify_work()
    updated = store.get_meeting(meeting_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    return updated


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] in store.ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Нельзя удалить встречу, пока она в очереди или в обработке",
        )
    if not store.delete_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    _purge_files(meeting_id)
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
    }


def _save_segments(meeting_id: str, document: dict, result: dict | None, *, replace_from: str | None = None, replace_to: str | None = None) -> dict:
    stored_result = result
    if stored_result is not None and replace_from and replace_to:
        stored_result = replace_speaker_label(stored_result, replace_from, replace_to)
    store.update_meeting(
        meeting_id,
        transcript=render_transcript(document),
        transcript_segments=document,
        result=stored_result,
    )
    updated = store.get_meeting(meeting_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    return updated


def _queue_protocol(meeting_id: str, message: str) -> dict:
    store.update_meeting(
        meeting_id,
        status="queued",
        status_message=message,
        error=None,
        result=None,
        progress=88,
        queued_at=store.utc_now(),
    )
    store.refresh_queue_messages()
    worker.notify_work()
    updated = store.get_meeting(meeting_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    return updated


@app.patch("/api/meetings/{meeting_id}/speakers/{speaker_id}")
def rename_meeting_speaker(
    meeting_id: str,
    speaker_id: str,
    payload: SpeakerNameIn,
    user: dict = Depends(require_user),
) -> dict:
    meeting = _owned(store.get_meeting(meeting_id), user)
    document = _segments_doc(meeting)
    try:
        updated, old_name, new_name = rename_speaker(document, speaker_id, payload.name)
    except SpeakerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = _save_segments(
        meeting_id,
        updated,
        meeting.get("result"),
        replace_from=old_name,
        replace_to=new_name,
    )
    if old_name != new_name:
        store.record_audit(_actor_name(user), "speaker.rename", meeting_id, f"{old_name} → {new_name}")
    return saved


@app.patch("/api/meetings/{meeting_id}/segments")
def reassign_meeting_segments(
    meeting_id: str,
    payload: SpeakerAssignIn,
    user: dict = Depends(require_user),
) -> dict:
    meeting = _owned(store.get_meeting(meeting_id), user)
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
    _save_segments(meeting_id, updated, None)
    store.record_audit(
        _actor_name(user),
        "speaker.reassign",
        meeting_id,
        f"{len(payload.segment_indexes)} реплик → {label}",
    )
    return _queue_protocol(meeting_id, "Обновляю протокол после правки спикеров")


@app.get("/api/meetings/{meeting_id}/export.md", response_class=PlainTextResponse)
def export_markdown(meeting_id: str, user: dict = Depends(require_user)) -> str:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    return to_markdown(meeting)


@app.get("/api/meetings/{meeting_id}/email.txt", response_class=PlainTextResponse)
def meeting_email_text(meeting_id: str, user: dict = Depends(require_user)) -> str:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    return email_body(meeting)


@app.post("/api/meetings/{meeting_id}/email")
def send_meeting_email(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    opened = open_or_save_eml(meeting)
    store.record_audit(
        _actor_name(user),
        "email.open",
        meeting_id,
        _EMAIL_OPENED.get(opened.get("mode") or "", "черновик сохранён"),
    )
    return opened


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
    if result.get("participants"):
        lines.append("- Участники: " + ", ".join(result["participants"]))
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
