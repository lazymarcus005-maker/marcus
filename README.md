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

cd web
npm install
npm run dev
```

`GET /healthz` — liveness (process up, no dependency checks).
`GET /readyz` — readiness (checks PostgreSQL, Redis, RabbitMQ connectivity).

The production React UI is served by the API at `/app` when `web/dist` is
present. The Vite dev server proxies API calls to `http://localhost:8000`.

## Marcus CLI

### Install a released version

Requires Python 3.12 or newer. Install the CLI into an isolated tool
environment with a pinned release:

```bash
uv tool install "marcus==0.1.0"
marcus --help
```

Alternatively, install the wheel asset from a GitHub release with `pipx`:

```bash
pipx install https://github.com/lazymarcus005-maker/marcus/releases/download/v0.1.0/marcus-0.1.0-py3-none-any.whl
```

Verify a downloaded release asset before installing:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

Upgrade or pin a specific release with `uv tool install --force
"marcus==<version>"`. The `marcus` command is provided by the package's
console entrypoint; the server and worker remain available from the same
distribution.

Run the local coding agent interactively:

```bash
uv run marcus
```

Run one prompt for scripts or CI:

```bash
uv run marcus -p "inspect the failing tests and summarize the cause"
```

CLI guardrails can be configured with environment variables using the
`HARNESS_` prefix:

```bash
HARNESS_CLI_MAX_TOTAL_TOKENS=50000
HARNESS_CLI_MAX_HISTORY_MESSAGES=100
HARNESS_CLI_HISTORY_SUMMARY_ENABLED=true
```

Inside the REPL, `/usage` shows calls, token totals, configured budget, and
remaining tokens. Use `/config` to inspect session LLM settings and `/exit` to
leave the session.

### Local CLI skills

The CLI discovers portable Markdown skills from `.marcus/skills/`. Each skill
is a directory containing `SKILL.md` with YAML frontmatter:

```text
.marcus/skills/code-review/SKILL.md
```

```markdown
---
name: code-review
description: Review changes for correctness and security
---

Inspect the diff first. Report concrete findings with file and line references.
```

Discovered skills appear in the system prompt. The model can call the
read-only `load_skill` tool with the exact skill name to load the full
instructions. Skill files are local project data; review them before trusting
their instructions.

## Running the full stack

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, RabbitMQ, the API, worker, scheduler, Jaeger, and
Prometheus. See `docker-compose.yml`, `docs/schema.md`, and `docs/runbook.md`
for details.
