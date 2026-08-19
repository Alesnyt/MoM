#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

env_val() {
  local key="$1" default="$2" line=""
  if [[ -f .env ]]; then
    line="$(grep -E "^${key}=" .env | tail -1 || true)"
    if [[ -n "$line" ]]; then
      printf '%s\n' "${line#*=}"
      return
    fi
  fi
  printf '%s\n' "$default"
}

HOST="${HOST:-$(env_val HOST 0.0.0.0)}"
PORT="${PORT:-$(env_val PORT 8000)}"

if [[ ! -d .venv ]]; then
  echo "Нет .venv. Сначала выполните: ./install.sh" >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [[ ! -f backend/static/index.html ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "Интерфейс не собран и нет Node.js. Выполните ./install.sh" >&2
    exit 1
  fi
  if [[ ! -d frontend/node_modules ]]; then
    (cd frontend && npm ci)
  fi
  (cd frontend && npm run build)
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "Установите ffmpeg и ffprobe (на Ubuntu: sudo apt-get install -y ffmpeg)." >&2
  exit 1
fi

port_busy="$(python - "$HOST" "$PORT" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
probe = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
sock = socket.socket()
sock.settimeout(0.3)
try:
    sock.connect((probe, port))
except OSError:
    print("0")
else:
    print("1")
finally:
    sock.close()
PY
)"

if [[ "$port_busy" == "1" ]]; then
  if [[ "${MOM_KILL_PORT:-0}" == "1" ]]; then
    if command -v fuser >/dev/null 2>&1; then
      fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
    elif command -v lsof >/dev/null 2>&1; then
      lsof -tiTCP:"$PORT" -sTCP:LISTEN | xargs kill >/dev/null 2>&1 || true
    fi
    sleep 1
  else
    echo "Порт ${PORT} занят. Остановите другой процесс или запустите MOM_KILL_PORT=1 ./start.sh" >&2
    exit 1
  fi
fi

echo "MoM: http://${HOST}:${PORT}"
if [[ "$HOST" == "0.0.0.0" ]]; then
  echo "С другой машины откройте http://<IP-этой-ВМ>:${PORT}"
  echo "Первый вход администратора требует SETUP_TOKEN из .env"
fi
exec python -m backend
