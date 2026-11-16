#!/bin/sh
set -eu
cd /app
mkdir -p data backups
# The directory mount permits atomic replacement. The old .env file remains a
# read-only bootstrap source and is imported automatically on the first start.
export MOM_ENV_PATH="${MOM_ENV_PATH:-/app/data/config/.env}"

python - <<'PY'
from pathlib import Path
import secrets
import os

from dotenv import dotenv_values
from backend import config

source = Path(".env") if Path(".env").is_file() else Path(".env.example")
config.initialize_env(source)
current = os.getenv("SETUP_TOKEN", "") or dotenv_values(config.ENV_PATH, interpolate=False).get("SETUP_TOKEN", "")
if current:
    raise SystemExit(0)
token = secrets.token_urlsafe(18)
config._upsert_env({"SETUP_TOKEN": token})
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
  exec runuser -u mom -- python -m backend
fi
exec python -m backend
