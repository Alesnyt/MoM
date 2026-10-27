# syntax=docker/dockerfile:1

FROM node:20-bookworm-slim AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        adduser \
        ffmpeg \
        ca-certificates \
        libgomp1 \
        libsndfile1 \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" --uid 1000 mom

WORKDIR /app

COPY requirements.txt requirements-gigaam.txt requirements-diarize.txt ./
# CPU wheels. The default PyPI torch build pulls CUDA and several extra gigabytes.
ARG INSTALL_DIARIZE=0
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DIARIZE" = "1" ]; then pip install --no-cache-dir -r requirements-diarize.txt; fi

COPY backend ./backend
COPY .env.example ./
COPY deploy/docker-entrypoint.sh /entrypoint.sh
COPY --from=ui /src/backend/static ./backend/static
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
ENTRYPOINT ["/entrypoint.sh"]
