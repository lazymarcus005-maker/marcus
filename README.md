# marcus

Hermes-style AI agent harness — a stateless agent runtime with PostgreSQL as the
source of truth for run state, skills, and audit history.

See `idea.md` for the design, `decisions.md` for the technical decisions taken,
and `tasks.md` for the implementation plan (tracked as GitHub issues).

## Stack

Python 3.12 + FastAPI, PostgreSQL, Redis, RabbitMQ. See `decisions.md` D1–D4.

## Development

```bash
uv sync --all-extras --dev
cp .env.example .env

uv run ruff check .
uv run ruff format --check .
uv run mypy harness
uv run pytest --cov=harness

uv run uvicorn harness.api.app:app --reload
```

`GET /healthz` — liveness (process up, no dependency checks).
`GET /readyz` — readiness (checks PostgreSQL, Redis, RabbitMQ connectivity).

## Running the full stack

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, RabbitMQ, the API, and the worker. See
`docker-compose.yml` and `docs/` for details.
