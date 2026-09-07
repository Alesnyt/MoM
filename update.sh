#!/usr/bin/env bash
# Pull Git changes, refresh Python/UI, keep .env and meeting data.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

NO_PULL=0
for arg in "$@"; do
  case "$arg" in
    --no-pull) NO_PULL=1 ;;
    -h|--help)
      echo "Usage: ./update.sh [--no-pull]"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }

if [[ ! -d .venv ]]; then
  echo "Нет .venv. Сначала выполните: ./install.sh" >&2
  exit 1
fi

if [[ "$NO_PULL" -eq 0 ]]; then
  if [[ ! -d .git ]]; then
    echo "Это не git-клон, пропускаю pull. Положите обновления вручную и запустите: ./update.sh --no-pull" >&2
  else
    log "Забираю изменения с GitHub"
    git pull --ff-only
  fi
fi

# shellcheck disable=SC1091
source .venv/bin/activate
log "Обновляю Python-зависимости, включая Whisper и GigaAM (PyTorch)"
python -m pip install -r requirements.txt

if [[ -f .env.example ]]; then
  python - <<'PY'
from pathlib import Path

env_path = Path(".env")
example = Path(".env.example")
if not example.exists():
    raise SystemExit(0)
existing = env_path.read_text() if env_path.exists() else ""
seen = set()
for line in existing.splitlines():
    if "=" in line and not line.strip().startswith("#"):
        seen.add(line.split("=", 1)[0])
added = []
for line in example.read_text().splitlines():
    if "=" not in line or line.strip().startswith("#"):
        continue
    key = line.split("=", 1)[0]
    if key and key not in seen:
        added.append(line)
        seen.add(key)
if added:
    with env_path.open("a") as fh:
        fh.write("\n" + "\n".join(added) + "\n")
    print("В .env добавлены новые ключи: " + ", ".join(item.split("=", 1)[0] for item in added))
else:
    print("Файл .env сохранён, существующие значения не трогались.")
PY
  chmod 600 .env 2>/dev/null || true
fi

log "Собираю интерфейс"
if ! command -v npm >/dev/null 2>&1; then
  echo "Нет Node.js/npm. Выполните ./install.sh --skip-model" >&2
  exit 1
fi
(cd frontend && npm ci && npm run build)
deactivate || true

if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled mom.service >/dev/null 2>&1; then
  log "Перезапускаю mom.service"
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl restart mom.service
  else
    sudo systemctl restart mom.service
  fi
  echo
  echo "Обновление готово, сервис перезапущен."
else
  echo
  echo "Обновление готово. Остановите старый ./start.sh (Ctrl+C) и запустите снова:"
  echo "  ./start.sh"
fi
echo "База, записи и .env не удалялись."
