FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.155.1
RUN npm install --global @openai/codex@${CODEX_VERSION}
FROM ghcr.io/astral-sh/uv:0.9.6 AS uv
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
ARG APP_UID=1000
ARG APP_GID=1000
RUN test "$APP_UID" -gt 0 && test "$APP_GID" -gt 0 && \
    ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex && \
    groupadd --non-unique --gid "$APP_GID" agent && \
    useradd --non-unique --create-home --uid "$APP_UID" --gid "$APP_GID" agent && \
    mkdir /auth && chown agent:agent /auth
WORKDIR /app
ENV HOME=/auth PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" CODEX_HOME=/auth
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY shared shared
COPY agent agent
COPY config config
USER agent
CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8001"]
