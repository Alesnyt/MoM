from __future__ import annotations

import asyncio
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, keys, store
from .audio import ffmpeg_available
from .config import AUDIO_DIR, UPLOAD_DIR, ensure_dirs
from .pipeline import format_duration, process_meeting

ALLOWED_SUFFIXES = {".webm", ".mp4", ".mp3", ".wav", ".m4a", ".ogg"}
STATIC_DIR = Path(__file__).resolve().parent / "static"
_setup_lock = threading.Lock()
_LOOPBACK = {"127.0.0.1", "::1", "testclient", "localhost"}


class SettingsIn(BaseModel):
    openai_api_key: str = Field(min_length=8)


class ModelsIn(BaseModel):
    chat_model: str | None = None
    asr_model: str | None = None


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    setup_token: str = ""


class UserLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    archive_limit: int = 5


class UserLimitIn(BaseModel):
    archive_limit: int = Field(ge=1, le=100)


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"


def _cookie_kw(request: Request) -> dict:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    secure = request.url.scheme == "https" or proto == "https"
    return {
        "httponly": True,
        "samesite": "lax",
        "max_age": auth.SESSION_SECONDS,
        "path": "/",
        "secure": secure,
    }


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


def _health(*, full: bool = False) -> dict:
    return {
        "ok": True,
        "ffmpeg": ffmpeg_available(),
        "ui": (STATIC_DIR / "index.html").exists(),
        "openai": keys.snapshot() if full else keys.public_snapshot(),
    }

@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    store.init_db()
    store.fail_interrupted_meetings()
    config.migrate_token_plan_asr()
    if config.get_api_key():
        try:
            await asyncio.wait_for(keys.verify_key(), timeout=float(config.VERIFY_TIMEOUT_SECONDS) * 3)
        except Exception:
            keys.reset_status("Не удалось проверить ключ при запуске — сохраните его снова в админке")
    else:
        keys.reset_status()
    yield


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
    response.set_cookie(auth.COOKIE_NAME, token, **_cookie_kw(request))
    return {"configured": True, "authenticated": True, "username": username, "setup_token_required": False}


@app.post("/api/auth/login")
def auth_login(payload: LoginIn, request: Request, response: Response) -> dict:
    _rate_or_429(request, "admin")
    if not config.admin_configured():
        raise HTTPException(status_code=400, detail="Сначала создайте учётную запись администратора")
    username = payload.username.strip()
    if not auth.check_credentials(username, payload.password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = auth.create_session(username)
    response.set_cookie(auth.COOKIE_NAME, token, **_cookie_kw(request))
    return {"configured": True, "authenticated": True, "username": username, "setup_token_required": False}


@app.post("/api/auth/logout")
def auth_logout(response: Response, mom_admin: str | None = Cookie(default=None)) -> dict:
    auth.drop_session(mom_admin)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
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
    record = store.get_user_auth(email)
    stored = record["password_hash"] if record else auth.dummy_hash()
    ok = auth.verify_password(payload.password, stored)
    if not record or not ok:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    token = auth.create_user_session(record["id"], record["email"])
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
def user_logout(response: Response, mom_user: str | None = Cookie(default=None)) -> dict:
    auth.drop_user_session(mom_user)
    response.delete_cookie(auth.USER_COOKIE, path="/")
    return {"authenticated": False, "email": None, "archive_limit": None}


@app.get("/api/admin/users")
def admin_list_users(_admin: dict = Depends(require_admin)) -> list[dict]:
    return store.list_users()


@app.post("/api/admin/users")
def admin_create_user(payload: UserCreateIn, _admin: dict = Depends(require_admin)) -> dict:
    email = _normalize_email(payload.email)
    if not _valid_email(email):
        raise HTTPException(status_code=400, detail="Укажите корректный email")
    if store.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="Пользователь с таким email уже есть")
    password = auth.generate_password()
    try:
        user = store.create_user(email, auth.hash_password(password), payload.archive_limit)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Пользователь с таким email уже есть")
    return {"user": user, "password": password}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: str,
    payload: UserLimitIn,
    _admin: dict = Depends(require_admin),
) -> dict:
    user = store.update_user(user_id, archive_limit=payload.archive_limit)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    removed = store.prune_user_meetings(user_id)
    for meeting_id in removed:
        _purge_files(meeting_id)
    user = store.get_user(user_id)
    return user


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, _admin: dict = Depends(require_admin)) -> dict:
    if not store.get_user(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    password = auth.generate_password()
    store.update_user(user_id, password_hash=auth.hash_password(password))
    auth.drop_user_sessions_for(user_id)
    return {"password": password}


@app.get("/api/settings")
def get_settings(_admin: dict = Depends(require_admin)) -> dict:
    return _health(full=True)


@app.post("/api/settings")
async def save_settings(payload: SettingsIn, _admin: dict = Depends(require_admin)) -> dict:
    key = payload.openai_api_key.strip()
    if " " in key or len(key) < 20:
        raise HTTPException(status_code=400, detail="Похоже, это не API-ключ")
    config.set_openai_api_key(key)
    openai_status = await keys.verify_key(key)
    return {"ok": True, "ffmpeg": ffmpeg_available(), "openai": openai_status}


@app.post("/api/settings/models")
def save_models(payload: ModelsIn, _admin: dict = Depends(require_admin)) -> dict:
    chat = (payload.chat_model or config.get_chat_model()).strip()
    asr = (payload.asr_model or config.get_asr_model()).strip()
    if not chat or not asr:
        raise HTTPException(status_code=400, detail="Укажите модели чата и расшифровки")
    config.apply_connection(config.get_base_url(), chat, asr)
    return _health(full=True)


@app.post("/api/settings/verify")
async def verify_settings(_admin: dict = Depends(require_admin)) -> dict:
    if not config.get_api_key():
        raise HTTPException(status_code=400, detail="Сначала сохраните ключ")
    openai_status = await keys.verify_key()
    return {"ok": True, "ffmpeg": ffmpeg_available(), "openai": openai_status}


@app.delete("/api/settings")
def delete_settings(_admin: dict = Depends(require_admin)) -> dict:
    config.clear_openai_api_key()
    keys.reset_status("Ключ удалён")
    return _health(full=True)


@app.get("/api/meetings")
def list_meetings(user: dict = Depends(require_user)) -> list[dict]:
    return store.list_meetings(user["user_id"])


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    return _owned(store.get_meeting(meeting_id), user)


@app.post("/api/meetings")
async def create_meeting(
    background_tasks: BackgroundTasks,
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
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Пустой файл")

    display_title = (title or "").strip() or Path(original).stem
    meeting = store.create_meeting(meeting_id, display_title, original, user["user_id"])
    for extra_id in store.prune_user_meetings(user["user_id"]):
        _purge_files(extra_id)
    background_tasks.add_task(process_meeting, meeting_id)
    return meeting


@app.post("/api/meetings/{meeting_id}/retry")
async def retry_meeting(
    meeting_id: str,
    background_tasks: BackgroundTasks,
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
    store.update_meeting(
        meeting_id,
        status="queued",
        status_message="Повторная обработка в очереди",
        error=None,
        transcript=None,
        result=None,
        language=None,
        progress=0,
    )
    background_tasks.add_task(process_meeting, meeting_id)
    updated = store.get_meeting(meeting_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    return updated


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, user: dict = Depends(require_user)) -> dict:
    _owned(store.get_meeting(meeting_id), user)
    if not store.delete_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Встреча не найдена")
    _purge_files(meeting_id)
    return {"ok": True}


@app.get("/api/meetings/{meeting_id}/export.md", response_class=PlainTextResponse)
def export_markdown(meeting_id: str, user: dict = Depends(require_user)) -> str:
    meeting = _owned(store.get_meeting(meeting_id), user)
    if meeting["status"] != "done" or not meeting.get("result"):
        raise HTTPException(status_code=409, detail="Протокол ещё не готов")
    return to_markdown(meeting)


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


if (STATIC_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
