#!/bin/sh
set -eu
cd /app
mkdir -p data backups

if [ ! -f .env ]; then
  cp .env.example .env
fi

python - <<'PY'
from pathlib import Path
import re
import secrets

path = Path(".env")
text = path.read_text()
match = re.search(r"^SETUP_TOKEN=(.*)$", text, re.M)
current = match.group(1).strip() if match else ""
if current:
    raise SystemExit(0)
token = secrets.token_urlsafe(18)
line = f"SETUP_TOKEN={token}"
if match:
    text = re.sub(r"^SETUP_TOKEN=.*$", line, text, count=1, flags=re.M)
else:
    text = text.rstrip() + "\n" + line + "\n"
path.write_text(text)
print(f"SETUP_TOKEN={token}")
print("Сохраните токен: он нужен для первого входа администратора.")
PY

# Inside the container the process must listen on all interfaces.
# load_dotenv does not override variables that are already set.
export HOST=0.0.0.0
export PORT="${PORT:-8000}"

# Volumes may be root-owned. Chown them, then run the server as mom.
if [ "$(id -u)" = "0" ]; then
  chown -R mom:mom data backups
  if [ -f .env ]; then
    chown mom:mom .env || true
  fi
  exec runuser -u mom -- python -m backend
fi
exec python -m backend
