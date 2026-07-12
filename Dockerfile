FROM node:22-alpine AS web-build

WORKDIR /web
COPY web/package*.json ./
RUN npm install
COPY web ./
RUN npm run build

FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY harness ./harness
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=web-build /web/dist ./web/dist

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "harness.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
CMD ["python", "-m", "harness.workers.main"]
