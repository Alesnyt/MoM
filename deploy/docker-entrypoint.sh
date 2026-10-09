#!/bin/sh
set -eu
cd /app
mkdir -p data

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
exec python -m backend
