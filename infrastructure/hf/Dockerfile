# Combined single-container build for a Hugging Face Space (Docker SDK). A Space runs one
# container and exposes one port, unlike the droplet's six-service compose stack
# (infrastructure/deployment/), so this Dockerfile pulls backend, frontend, ClamAV and an SSH
# tunnel to the droplet's Postgres into one image, run together by supervisord.
#
# The app source isn't copied into this repo: it's pulled from the wob repository at build time,
# pinned to SOURCE_REF, so this stays in sync with the real source of truth instead of drifting.
# Once infrastructure/hf/ lands on samuelmugisha/wob main, flip SOURCE_REPO back to it.
ARG SOURCE_REPO=https://github.com/Unity-oss/wob.git
ARG SOURCE_REF=deploy/hf-migration-r2-storage

FROM alpine/git:2.47.1 AS source
ARG SOURCE_REPO
ARG SOURCE_REF
RUN git clone --depth 1 --branch "$SOURCE_REF" "$SOURCE_REPO" /src

FROM python:3.12-slim AS backend-deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken
COPY --from=source /src/backend/pyproject.toml /src/backend/uv.lock ./
RUN uv sync --frozen --no-dev --extra local-embeddings --no-install-project
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng \
        clamav clamav-daemon \
        openssh-client autossh \
        supervisor curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --no-create-home --shell /usr/sbin/nologin wobapp \
    && mkdir -p /run/clamav /var/log/clamav /data/models /data/storage \
    && chown -R clamav:clamav /run/clamav /var/log/clamav \
    && chmod 750 /run/clamav

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    MODEL_CACHE_DIR=/data/models \
    STORAGE_DIR=/data/storage \
    CLAMD_HOST=127.0.0.1 \
    CLAMD_PORT=3310 \
    DB_TUNNEL_PORT=5432

COPY --from=backend-deps /opt/venv /opt/venv
COPY --from=backend-deps /opt/tiktoken /opt/tiktoken
COPY --from=source /src/backend /app/backend

WORKDIR /app/frontend
COPY --from=source /src/frontend/package.json /src/frontend/package-lock.json /src/frontend/next.config.ts ./
RUN npm ci --omit=dev && npm cache clean --force
# frontend-dist/.next is the real compiled build pulled from the running wob-web-1 container
# (docker cp), not built from SOURCE_REPO here: that repo's frontend/app/ is missing routes
# (judges-pack, judges-scores) added after the last commit that reached it -- see README.md.
# It isn't committed to the application repo (a 43MB binary build artifact doesn't belong in
# source control); it's uploaded straight to this Space, alongside this Dockerfile.
COPY frontend-dist/.next ./.next

# These support files live alongside this Dockerfile at the build context root: inside this repo
# that's infrastructure/hf/, but the actual HF Space has its own separate git repo with this
# Dockerfile and these files at its root (see infrastructure/hf/README.md) -- that's the real
# build context, so the COPY sources below have no infrastructure/hf/ prefix.
COPY clamd.conf /etc/clamav/clamd.conf
COPY freshclam.conf /etc/clamav/freshclam.conf
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY db-tunnel.sh /usr/local/bin/db-tunnel.sh
COPY wait-for.sh /usr/local/bin/wait-for.sh
RUN chmod +x /usr/local/bin/db-tunnel.sh /usr/local/bin/wait-for.sh

WORKDIR /app/backend
EXPOSE 7860
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
