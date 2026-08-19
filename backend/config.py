from pathlib import Path

from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "mom.db"
ENV_PATH = ROOT / ".env"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip()
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "").strip()
ADMIN_USER = os.getenv("ADMIN_USER", "").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


HOST = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = _int_env("PORT", 8000)
MAX_UPLOAD_BYTES = max(8 * 1024 * 1024, _int_env("MAX_UPLOAD_MB", 512) * 1024 * 1024)
VERIFY_TIMEOUT_SECONDS = max(5, _int_env("VERIFY_TIMEOUT_SECONDS", 25))
FFMPEG_TIMEOUT_SECONDS = max(60, _int_env("FFMPEG_TIMEOUT_SECONDS", 1800))
WHISPER_DIR = DATA_DIR / "whisper"

MAX_WHISPER_BYTES = 24 * 1024 * 1024
CHUNK_SECONDS = 12 * 60
QWEN_BASE_INTL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_BASE_CN = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_CHAT_MODELS = (
    "qwen3.7-plus",
    "qwen3.7-flash",
    "qwen3.8-max",
    "qwen-plus",
    "qwen-flash",
    "qwen-turbo",
)
QWEN_ASR_MODEL = "qwen3-asr-flash"
PAYG_ASR_MODELS = (
    "qwen3-asr-flash",
    "qwen3-asr-flash-2026-02-10",
    "qwen-audio-3.0-asr-flash",
)
TOKEN_PLAN_ASR_MODELS = ("qwen-audio-3.0-asr-flash",)
LOCAL_ASR_MODEL = "local-whisper"
QWEN_ASR_MODELS = TOKEN_PLAN_ASR_MODELS + PAYG_ASR_MODELS
TOKEN_PLAN_BASE = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"


def ensure_dirs() -> None:
    for path in (DATA_DIR, UPLOAD_DIR, AUDIO_DIR, WHISPER_DIR):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(DATA_DIR / "hf"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def detect_provider(key: str | None = None) -> str:
    secret = (key if key is not None else OPENAI_API_KEY).strip()
    if secret.startswith(("sk-ws-", "sk-sp-")):
        return "qwen"
    if OPENAI_BASE_URL and "dashscope" in OPENAI_BASE_URL:
        return "qwen"
    if secret:
        return "openai"
    return "none"


def provider_label(provider: str | None = None) -> str:
    name = provider or detect_provider()
    if name == "qwen":
        return "Qwen"
    if name == "openai":
        return "OpenAI"
    return "API"


def default_base_url(key: str) -> str:
    if key.startswith("sk-sp-"):
        return TOKEN_PLAN_BASE
    if key.startswith("sk-ws-"):
        return QWEN_BASE_INTL
    return ""


def is_token_plan() -> bool:
    return OPENAI_API_KEY.startswith("sk-sp-") or "token-plan" in (OPENAI_BASE_URL or "")


def default_chat_model() -> str:
    if is_token_plan() or detect_provider() == "qwen":
        return "qwen3.7-plus"
    return "gpt-4o"


def default_asr_model() -> str:
    if is_token_plan():
        return LOCAL_ASR_MODEL
    if detect_provider() == "qwen":
        return QWEN_ASR_MODEL
    return "whisper-1"


def asr_model_candidates(preferred: str | None = None) -> list[str]:
    first = preferred or get_asr_model()
    extras = TOKEN_PLAN_ASR_MODELS if is_token_plan() else QWEN_ASR_MODELS
    return [item for item in dict.fromkeys([first, *extras]) if item and not is_local_asr(item)]


def is_local_asr(model: str | None = None) -> bool:
    name = (model if model is not None else get_asr_model()).strip().lower()
    return name in {"local", LOCAL_ASR_MODEL, "faster-whisper"} or name.startswith("local-")


def local_whisper_size() -> str:
    allowed = {"tiny", "base", "small", "medium", "large-v2", "large-v3"}
    raw = os.getenv("LOCAL_WHISPER_SIZE", "").strip().lower()
    if raw in allowed:
        return raw
    current = get_asr_model().strip().lower()
    if current.startswith("local-") and current != LOCAL_ASR_MODEL:
        size = current.split("-", 1)[1]
        if size in allowed:
            return size
    return "small"


def is_payg_asr(model: str) -> bool:
    name = (model or "").strip().lower()
    return name in PAYG_ASR_MODELS or name.endswith("asr-flash")


def migrate_token_plan_asr() -> None:
    if not is_token_plan():
        return
    current = get_asr_model()
    if is_local_asr(current) or current == "qwen-audio-3.0-asr-flash":
        return
    apply_connection(get_base_url(), get_chat_model(), default_asr_model())


def get_api_key() -> str:
    return OPENAI_API_KEY


def get_base_url() -> str:
    return OPENAI_BASE_URL or default_base_url(OPENAI_API_KEY)


def get_chat_model() -> str:
    if OPENAI_MODEL:
        return OPENAI_MODEL
    return default_chat_model()


def get_asr_model() -> str:
    if WHISPER_MODEL:
        return WHISPER_MODEL
    return default_asr_model()


def is_qwen() -> bool:
    return detect_provider() == "qwen"


def _upsert_env(values: dict[str, str]) -> None:
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()
    elif (ROOT / ".env.example").exists():
        lines = (ROOT / ".env.example").read_text().splitlines()
    else:
        lines = []
    seen: set[str] = set()
    next_lines: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.strip().startswith("#") else ""
        if key in values:
            next_lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            next_lines.append(line)
    for key, value in values.items():
        if key not in seen:
            next_lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(next_lines).rstrip() + "\n")


def set_runtime(key: str, base_url: str = "", chat_model: str = "", asr_model: str = "") -> None:
    global OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, WHISPER_MODEL
    OPENAI_API_KEY = key
    if base_url:
        OPENAI_BASE_URL = base_url
    if chat_model:
        OPENAI_MODEL = chat_model
    if asr_model:
        WHISPER_MODEL = asr_model
    os.environ["OPENAI_API_KEY"] = key
    if OPENAI_BASE_URL:
        os.environ["OPENAI_BASE_URL"] = OPENAI_BASE_URL
    if OPENAI_MODEL:
        os.environ["OPENAI_MODEL"] = OPENAI_MODEL
    if WHISPER_MODEL:
        os.environ["WHISPER_MODEL"] = WHISPER_MODEL


def set_openai_api_key(key: str) -> None:
    key = key.strip()
    payload = {"OPENAI_API_KEY": key}
    if key.startswith("sk-ws-") or key.startswith("sk-sp-"):
        payload["OPENAI_BASE_URL"] = default_base_url(key)
        if not (OPENAI_MODEL or "").startswith("qwen"):
            payload["OPENAI_MODEL"] = "qwen3.7-plus" if key.startswith("sk-sp-") else "qwen-plus"
        if key.startswith("sk-sp-"):
            payload["WHISPER_MODEL"] = LOCAL_ASR_MODEL
        elif not (WHISPER_MODEL or "").startswith("qwen"):
            payload["WHISPER_MODEL"] = QWEN_ASR_MODEL
    set_runtime(
        key,
        payload.get("OPENAI_BASE_URL", OPENAI_BASE_URL),
        payload.get("OPENAI_MODEL", OPENAI_MODEL),
        payload.get("WHISPER_MODEL", WHISPER_MODEL),
    )
    _upsert_env(payload)


def apply_connection(base_url: str, chat_model: str, asr_model: str) -> None:
    global OPENAI_BASE_URL, OPENAI_MODEL, WHISPER_MODEL
    OPENAI_BASE_URL = base_url
    OPENAI_MODEL = chat_model
    WHISPER_MODEL = asr_model
    os.environ["OPENAI_BASE_URL"] = base_url
    os.environ["OPENAI_MODEL"] = chat_model
    os.environ["WHISPER_MODEL"] = asr_model
    _upsert_env(
        {
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "OPENAI_BASE_URL": base_url,
            "OPENAI_MODEL": chat_model,
            "WHISPER_MODEL": asr_model,
        }
    )


def clear_openai_api_key() -> None:
    set_openai_api_key("")


def admin_configured() -> bool:
    return bool(ADMIN_USER and ADMIN_PASSWORD_HASH)


def get_admin_user() -> str:
    return ADMIN_USER


def get_admin_password_hash() -> str:
    return ADMIN_PASSWORD_HASH


def set_admin_credentials(username: str, password_hash: str) -> None:
    global ADMIN_USER, ADMIN_PASSWORD_HASH
    ADMIN_USER = username.strip()
    ADMIN_PASSWORD_HASH = password_hash.strip()
    os.environ["ADMIN_USER"] = ADMIN_USER
    os.environ["ADMIN_PASSWORD_HASH"] = ADMIN_PASSWORD_HASH
    _upsert_env({"ADMIN_USER": ADMIN_USER, "ADMIN_PASSWORD_HASH": ADMIN_PASSWORD_HASH})
