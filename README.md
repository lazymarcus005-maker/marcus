# Marcus

Marcus is a local coding-agent CLI built on the Harness agent runtime. It can
read and edit files, run commands, manage long-running local processes, track
token usage, and apply risk-based approval rules while working inside the
current project.

The repository also contains the broader Harness stack: FastAPI, PostgreSQL,
Redis, RabbitMQ, workers, scheduling, skills, audit history, and a React web UI.

## What Is In This Repo

- `marcus_code/` - the local Marcus Code CLI and terminal/TUI experience.
- `harness/` - the server-side agent runtime, API, workers, tools, DB models,
  auth, MCP plumbing, and observability.
- `web/` - the React UI served by the API when built.
- `docs/` - schema, runbook, and design notes.
- `.github/workflows/` - CI and GitHub release automation.

See `idea.md`, `decisions.md`, and `tasks.md` for design history and planning.

## Requirements

- Python 3.12 or newer
- `uv`
- Node.js/npm for the web UI
- Docker, if running the full Harness stack locally

## Install Marcus CLI

Install the latest released wheel into an isolated `uv` tool environment:

```bash
uv tool install --force https://github.com/lazymarcus005-maker/marcus/releases/download/v2.1.5/marcus-2.1.5-py3-none-any.whl
marcus --version
marcus --help
```

For unreleased work from this checkout, run locally:

```bash
uv sync --all-extras --dev
uv run marcus
```

Or install the checkout into the local `uv` tool environment:

```bash
uv tool install --force .
```

Run one prompt non-interactively:

```bash
uv run marcus -p "inspect the failing tests and summarize the cause"
```

Launch the Textual TUI:

```bash
uv run marcus tui
```

## First-Time Config

Marcus defaults to Ollama Cloud:

```text
https://ollama.com/v1
```

Start `marcus` and run:

```text
/config edit
```

Marcus will ask for:

- LLM base URL
- API key
- model

Config is saved to:

```text
~/.marcus/config.toml
```

Environment variables with the `HARNESS_` prefix override the user config:

```bash
HARNESS_LLM_BASE_URL=https://ollama.com/v1
HARNESS_LLM_API_KEY=...
HARNESS_LLM_MODEL=gpt-oss:120b
HARNESS_LLM_REASONING_EFFORT=auto
HARNESS_LLM_MAX_COMPLETION_TOKENS=4096
```

Do not commit API keys or paste them into logs. If a key is exposed, rotate it.

## CLI Commands

Inside the REPL:

```text
/help
/status
/usage
/usage login
/model llama-3.1-70b
/effort high
/config
/config edit
/mode auto
/compact
/clear
/retry
/continue
/save
/exit
```

Common workflows:

- `/config edit` switches provider, API key, and model.
- `/model <name>` switches and persists the default model.
- `/effort <level>` switches and persists reasoning effort.
- `/usage` shows token totals and, for Ollama Cloud, quota information.
- `/usage login` opens a browser once to save Ollama Cloud usage-session state.
- `/mode ask|agent|auto|yolo` changes tool approval behavior.

## Reasoning Effort

Marcus supports first-class reasoning effort values:

```text
off
low
medium
high
auto
```

`auto` leaves model behavior unchanged. Other levels add a lightweight system
hint for the current LLM call. `HARNESS_LLM_MAX_COMPLETION_TOKENS`, when set, is
sent as `max_completion_tokens` to OpenAI-compatible chat-completions endpoints.

This is the first layer of the feature. Provider-specific adapters for models
with special thinking controls are tracked in `feature-worl.md`.

## Agent Modes

Marcus has four local operating modes:

- `ask` - inspect and explain only; writes and commands are blocked.
- `agent` - normal risk-based approval flow.
- `auto` - edit and test automatically; asks for high-risk commands.
- `yolo` - bypasses approvals while keeping hard guardrails active.

Use:

```bash
uv run marcus --mode auto
```

or inside the REPL:

```text
/mode auto
```

## Local Skills

Marcus discovers portable Markdown skills from `.marcus/skills/`.

Example:

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

Discovered skills appear in the system prompt. The model can call the read-only
`load_skill` tool with the exact skill name to load full instructions.

## Development

Set up the Python environment:

```bash
uv sync --all-extras --dev
```

Run checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy harness
uv run pytest -q
```

Run the API:

```bash
uv run uvicorn harness.api.app:app --reload
```

Run the web UI:

```bash
cd web
npm install
npm run dev
```

The Vite dev server proxies API calls to `http://localhost:8000`.

## Full Stack

Run the full Harness stack:

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, RabbitMQ, the API, worker, scheduler, Jaeger, and
Prometheus.

Health endpoints:

```text
GET /healthz
GET /readyz
```

When `web/dist` exists, the production React UI is served by the API at `/app`.

## Build And Release

Build distributions locally:

```bash
uv build
```

Releases are created by pushing a `v*` tag. The GitHub Actions release workflow
builds the wheel and sdist, generates `SHA256SUMS`, and uploads the assets to a
GitHub release.

```bash
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push origin main
git push origin vX.Y.Z
```

Verify a downloaded release asset:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

## Notes

- Marcus currently uses OpenAI-compatible chat completions.
- Tool calling is required for agentic coding workflows.
- Some providers omit usage fields; Marcus estimates token usage in that case.
- Full provider-specific reasoning adapters are planned but not complete.
