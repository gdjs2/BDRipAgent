FROM ghcr.io/astral-sh/uv:0.9.6 AS uv
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends handbrake-cli mkvtoolnix ffmpeg mediainfo fonts-liberation fonts-liberation2 fonts-dejavu-core ca-certificates git && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY shared shared
COPY backend backend
COPY worker worker
COPY agent agent
COPY profiles profiles
COPY config config
# Separate locked environments: CRF Studio needs PyAV 18, Sup2Sup needs PyAV <18.
ARG BDRIP_SCRIPTS_REV=529552b7b219f3fb9d7df8ce651c4e5fb265d484
ARG SUP2SUP_REV=608477404527f68d7c4764f694a01118b39c67ba
RUN git init /opt/crf-studio && git -C /opt/crf-studio remote add origin https://github.com/gdjs2/BDRip_Scripts.git && \
    git -C /opt/crf-studio fetch --depth 1 origin "$BDRIP_SCRIPTS_REV" && git -C /opt/crf-studio checkout FETCH_HEAD && \
    uv sync --frozen --no-dev --project /opt/crf-studio --python /usr/local/bin/python
RUN git init /opt/sup2sup && git -C /opt/sup2sup remote add origin https://github.com/gdjs2/Sup2Sup.git && \
    git -C /opt/sup2sup fetch --depth 1 origin "$SUP2SUP_REV" && git -C /opt/sup2sup checkout FETCH_HEAD && \
    uv sync --frozen --no-dev --project /opt/sup2sup --python /usr/local/bin/python
# Module startup keeps /app on sys.path for imports made later by worker tasks.
CMD ["python", "-m", "celery", "-A", "worker.tasks:celery", "worker", "--loglevel=INFO", "--autoscale=8,1"]
