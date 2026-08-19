#!/usr/bin/env bash
# Install OS packages, Python deps, frontend, and (optionally) Whisper model.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

DOWNLOAD_MODEL=1
INSTALL_SYSTEMD=0
for arg in "$@"; do
  case "$arg" in
    --skip-model) DOWNLOAD_MODEL=0 ;;
    --systemd) INSTALL_SYSTEMD=1 ;;
    -h|--help)
      echo "Usage: ./install.sh [--skip-model] [--systemd]"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif have sudo; then
    sudo "$@"
  else
    echo "Нужны права root для установки пакетов: $*" >&2
    exit 1
  fi
}

python_ok() {
  local bin="$1"
  "$bin" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

ensure_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if have "$candidate" && python_ok "$candidate"; then
      PYTHON="$candidate"
      return
    fi
  done
  echo "Нужен Python 3.10+ (лучше 3.11/3.12)." >&2
  exit 1
}

install_apt() {
  as_root apt-get update -y
  as_root DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-dev python3-pip \
    build-essential pkg-config \
    ffmpeg \
    curl ca-certificates \
    libgomp1
  install_node_linux
}

install_dnf() {
  as_root dnf install -y \
    python3 python3-devel python3-pip \
    gcc gcc-c++ make pkgconf \
    ffmpeg \
    curl ca-certificates \
    libgomp
  install_node_linux
}

node_major() {
  node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0
}

install_node_linux() {
  if have node && [[ "$(node_major)" -ge 18 ]]; then
    return
  fi
  if have apt-get; then
    as_root apt-get install -y ca-certificates curl gnupg
    as_root mkdir -p /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | as_root gpg --batch --yes --dearmor -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
      | as_root tee /etc/apt/sources.list.d/nodesource.list >/dev/null
    as_root apt-get update -y
    as_root apt-get install -y nodejs
  elif have dnf; then
    as_root dnf install -y nodejs npm
  elif have pacman; then
    as_root pacman -Sy --noconfirm --needed nodejs npm
  fi
}

install_pacman() {
  as_root pacman -Sy --noconfirm --needed \
    python python-pip \
    base-devel \
    ffmpeg \
    curl ca-certificates \
    gcc-libs \
    nodejs npm
}

install_brew() {
  have python3 || brew install python
  have ffmpeg || brew install ffmpeg
  have node || brew install node
}

ensure_node() {
  if have node && have npm; then
    local major
    major="$(node -p 'process.versions.node.split(".")[0]')"
    if [[ "$major" -ge 18 ]]; then
      return
    fi
    echo "Найден Node $(node -v), нужен 18+. Обновите Node.js." >&2
    exit 1
  fi
  echo "Не найден Node.js 18+ (нужен для сборки интерфейса)." >&2
  exit 1
}

ensure_ffmpeg() {
  if have ffmpeg && have ffprobe; then
    return
  fi
  echo "Не найден ffmpeg/ffprobe." >&2
  exit 1
}

log "Проверяю системные пакеты"
if have apt-get; then
  install_apt
elif have dnf; then
  install_dnf
elif have pacman; then
  install_pacman
elif have brew; then
  install_brew
else
  echo "Неизвестный дистрибутив. Установите вручную: Python 3.10+, ffmpeg, Node.js 18+." >&2
fi

ensure_python
ensure_ffmpeg
ensure_node

log "Python: $($PYTHON --version), Node: $(node -v), ffmpeg: $(ffmpeg -version | head -1)"

log "Создаю виртуальное окружение"
if [[ ! -d .venv ]]; then
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip wheel
python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  log "Копирую .env.example → .env"
  cp .env.example .env
fi

SETUP_TOKEN_VALUE="$(python - <<'PY'
from pathlib import Path
import re
import secrets

path = Path(".env")
text = path.read_text() if path.exists() else ""
match = re.search(r"^SETUP_TOKEN=(.*)$", text, re.M)
current = (match.group(1).strip() if match else "")
if current:
    print(current)
else:
    token = secrets.token_urlsafe(18)
    line = f"SETUP_TOKEN={token}"
    if match:
        text = re.sub(r"^SETUP_TOKEN=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    path.write_text(text)
    print(token)
PY
)"
chmod 600 .env 2>/dev/null || true

log "Собираю интерфейс"
(cd frontend && npm ci && npm run build)

if [[ "$DOWNLOAD_MODEL" -eq 1 ]]; then
  log "Скачиваю модель Whisper small в data/whisper (~500 МБ, один раз)"
  python - <<'PY'
from faster_whisper import WhisperModel
from backend.config import WHISPER_DIR, ensure_dirs

ensure_dirs()
print(f"Каталог модели: {WHISPER_DIR}")
WhisperModel("small", device="cpu", compute_type="int8", download_root=str(WHISPER_DIR))
print("Модель Whisper готова.")
PY
fi

if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "systemd доступен только на Linux." >&2
    exit 1
  fi
  unit_src="$ROOT/deploy/mom.service"
  unit_dst="/etc/systemd/system/mom.service"
  tmp="$(mktemp)"
  sed \
    -e "s|REPLACE_USER|$(id -un)|g" \
    -e "s|REPLACE_GROUP|$(id -gn)|g" \
    -e "s|REPLACE_ROOT|$ROOT|g" \
    "$unit_src" > "$tmp"
  as_root cp "$tmp" "$unit_dst"
  rm -f "$tmp"
  as_root systemctl daemon-reload
  as_root systemctl enable --now mom.service
  log "Сервис mom.service включён"
fi

deactivate || true

echo
echo "Готово. Дальше:"
echo "  1. Токен первого входа администратора (SETUP_TOKEN):"
echo "     ${SETUP_TOKEN_VALUE}"
echo "     Сохраните его: без него с другой машины админа не создать."
echo "  2. При необходимости отредактируйте .env (HOST, PORT, ключ API)."
echo "  3. Запуск: ./start.sh"
echo "  4. Откройте http://<IP-виртуальной-машины>:8000"
echo "  Не публикуйте порт 8000 в интернет без TLS-прокси."
