ARG DOCKER_PROXY=

FROM ${DOCKER_PROXY}node:20-bookworm-slim AS ui
COPY deploy/corporate/ca/ /usr/local/share/ca-certificates/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/*
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ${DOCKER_PROXY}python:3.12-slim-bookworm

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

COPY deploy/corporate/ca/ /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt
WORKDIR /app

COPY requirements.txt requirements-gigaam.txt requirements-diarize.txt ./
# Optional CPython 3.12 Linux CPU wheels; a clean checkout uses the CPU index.
COPY deploy/corporate/constraints.txt /app/constraints.txt
COPY deploy/corporate/wheels/ /tmp/wheels/
ARG INSTALL_DIARIZE=1
RUN pip install --no-cache-dir --upgrade pip \
    && (set -- /tmp/wheels/*.whl; if [ -f "$1" ]; then pip install --no-cache-dir --no-deps "$@"; fi) \
    && pip install --no-cache-dir -c constraints.txt torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -c constraints.txt -r requirements.txt \
    && if [ "$INSTALL_DIARIZE" = "1" ]; then pip install --no-cache-dir -c constraints.txt -r requirements-diarize.txt; fi \
    && rm -rf /tmp/wheels

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
