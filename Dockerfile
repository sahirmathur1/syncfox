### syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
RUN if [ -f uv.lock ]; then uv sync --frozen --no-dev --no-install-project; \
    else uv sync --no-dev --no-install-project; fi
COPY src ./src
RUN if [ -f uv.lock ]; then uv sync --frozen --no-dev; \
    else uv sync --no-dev; fi


FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    HOME=/data \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini curl unzip age \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cloudsync && useradd --uid 10001 --gid cloudsync --no-create-home --shell /usr/sbin/nologin cloudsync \
    && mkdir -p /data && chown 10001:10001 /data

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY migrations ./migrations
USER cloudsync

EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["/app/.venv/bin/uvicorn","cloud_sync.main:app","--host","0.0.0.0","--port","8080"]
