FROM ghcr.io/astral-sh/uv:0.9.6 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg mediainfo fonts-noto-core fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY shared shared
COPY backend backend
COPY agent/prompts agent/prompts
COPY profiles profiles
COPY config config
COPY alembic.ini ./
COPY scripts/storage_permissions.py /app/scripts/storage_permissions.py
ARG APP_UID=1000
ARG APP_GID=1000
RUN test "$APP_UID" -gt 0 && test "$APP_GID" -gt 0 && \
    groupadd --non-unique --gid "$APP_GID" app && \
    useradd --non-unique --uid "$APP_UID" --gid "$APP_GID" --create-home app
ENV HOME=/home/app PYTHONDONTWRITEBYTECODE=1
USER app
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn backend.app.main:app --host 0.0.0.0 --port 8000"]
