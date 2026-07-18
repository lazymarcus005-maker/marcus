# Handoff: Marcus Code — CLI coding agent (Phase 1 MVP)

Audience: a fresh Claude session picking this up with no prior context.
Everything you need is in this doc + the repo. Do not re-litigate the
decisions in "Decisions already made" — they were assessed and agreed with
the user.

## Mission

Build **Marcus Code**: an interactive CLI coding agent (in the spirit of
Claude Code / OpenCode) as a new package `marcus_code/` in this repo,
reusing the agent-harness core where it makes sense. Phase 1 is a working
MVP; streaming/MCP-stdio/session-resume are explicitly later phases.

Invocation target: user runs `marcus` (or `uv run marcus`) inside any
project directory and chats with an agent that can read/search/edit files
in that directory and run shell commands with per-action confirmation.

## Context: what this repo is

`harness/` is a server-side multi-tenant agent orchestrator:
FastAPI API + RabbitMQ queue + worker processes + PostgreSQL as source of
truth. A `RunEngine` (harness/runtime/engine.py) drives a ReAct loop:
LLM call → tool calls → checkpoint to DB → repeat, with human approval
gates for risky tools, token/step guardrails, MCP tool integration
(streamable-HTTP only), skills, quotas, Slack integration, and a React
dashboard in `web/`.

The server stack stays untouched. Marcus Code is a *sibling* engine: it
runs the whole loop **in-process** in the CLI, no queue, no Postgres, no
worker.

## Decisions already made (do not revisit)

1. **In-process loop.** The CLI must work with zero infrastructure —
   no Postgres/RabbitMQ/Redis. Conversation state lives in memory for
   Phase 1 (persistence is a later phase).
2. **Do NOT reuse `RunEngine` directly.** It is tightly coupled to
   AsyncSession/RunRepository/DB checkpointing. Write a new lightweight
   loop in `marcus_code/` that reuses the *pieces* below.
3. **Tools operate on the user's cwd**, not the server sandbox. Path
   containment: tools must refuse to touch files outside the directory
   the CLI was started in (same `resolve()`/`is_relative_to` pattern as
   `_resolve_sandboxed_path` in harness/runtime/native_tools.py).
4. **Approval = terminal y/n prompt**, not the DB approval flow. Risk
   tiers still drive it: `read_only` tools run without asking;
   write/execute tools prompt before each call (with a "always allow
   this tool for this session" option, like Claude Code's "don't ask
   again").
5. **Reuse the OpenAI-compatible LLM gateway** (`harness/llm/gateway.py`)
   as-is for Phase 1 (non-streaming). Streaming is Phase 2.
6. **Config via env vars first**: `HARNESS_LLM_BASE_URL`,
   `HARNESS_LLM_API_KEY`, `HARNESS_LLM_MODEL` (already defined in
   harness/config.py Settings). A `~/.marcus/config` file is a later
   phase. The user runs Ollama Cloud (`https://ollama.com/v1`, model
   `gpt-oss:120b`) — ask them for the API key when testing; never
   hardcode keys.

## Reuse map (exact paths)

| Reuse | From | Notes |
|---|---|---|
| LLM client (retries, tool-call parsing) | `harness/llm/gateway.py`, `harness/llm/types.py` | Use as-is. `LLMGateway(settings=...)`, `complete(messages, tools=...)` |
| `Tool` dataclass + `ToolHandler` + risk tiers | `harness/runtime/tools.py`, `harness/db/enums.py` (RiskTier) | Import directly |
| Guardrail concepts (max steps, repeated-call detection) | `harness/runtime/guardrails.py` | `check_repeated_calls` is DB-bound — reimplement in-memory; `APPROVAL_REQUIRED_TIERS` is importable |
| Result truncation | `harness/runtime/result_pipeline.py` → `truncate_result(value, max_chars=)` | Pure function, reuse |
| HTML-stripping fetch_url | `harness/runtime/native_tools.py` (`build_fetch_url_tool`, `_strip_html`) | Reusable if you add fetch to the CLI toolset |
| Message shaping for the LLM | `harness/llm/types.py` `LLMMessage` | Build your own history list; see `harness/runtime/context.py` for the role conventions used |

## Phase 1 deliverables

New package `marcus_code/` (same repo, same `pyproject.toml`):

1. **Entry point** — add to `pyproject.toml`:
   `[project.scripts] marcus = "marcus_code.cli:main"`.
   `main()` starts a REPL bound to `Path.cwd()`.
2. **`marcus_code/loop.py`** — the in-process ReAct loop:
   history in memory, call gateway, dispatch tool calls, feed results
   back, stop on `finish` or plain-text reply (plain text = show to user
   and wait for next input). Enforce max-steps (default 25) and an
   in-memory repeated-identical-call guard.
3. **`marcus_code/tools.py`** — cwd-scoped toolset:
   - `read_file(path)` — read_only
   - `write_file(path, content)` — sensitive_write (prompts)
   - `edit_file(path, old_string, new_string)` — sensitive_write
     (prompts, shows a unified diff before asking; fail if old_string
     not found or not unique)
   - `list_files(glob_pattern)` — read_only (respect .gitignore is nice-to-have, cap results)
   - `grep(pattern, glob?)` — read_only (cap results/output size)
   - `run_cli(command)` — destructive (always prompts; reuse the
     asyncio.create_subprocess_shell + timeout pattern from
     `build_run_cli_tool` in native_tools.py, cwd = repo root)
   - `fetch_url(url)` — read_only (reuse harness implementation)
4. **`marcus_code/ui.py`** — terminal UX with `rich`
   (add `rich>=13` to dependencies): user prompt, assistant markdown
   rendering, tool-call lines (name + args summary), diff highlighting
   for edit_file, y/n/a (yes/no/always) approval prompt, Ctrl+C
   cancels the current turn without exiting.
5. **System prompt** — short, coding-agent flavored: you are Marcus
   Code, working directory is X, prefer edit_file over write_file for
   existing files, ask before destructive actions, etc.
6. **Tests** — `tests/test_marcus_code_*.py`, unit-level, **no
   infrastructure required** (fake LLM gateway like `tests/fakes.py`'s
   `ScriptedLLMGateway`, tmp_path for file tools, monkeypatched
   approval callback). Follow existing test style.

Out of scope for Phase 1 (do not build): streaming, MCP (either
transport), skills, session persistence/resume, slash commands, config
files, web dashboard changes, any harness/ server changes beyond
extracting genuinely shared helpers (if you must move code, keep the
old import path working).

## Conventions & environment

- Windows 11 dev machine, PowerShell + Git Bash, Python 3.13, **uv**
  (`uv run pytest`, `uv sync`). Line endings: repo warns LF→CRLF, ignore.
- Code style: match harness/ — async-first, dataclasses, type hints,
  short docstrings explaining *why*, no decorative comments.
- Run unit tests without infra:
  `uv run pytest tests/test_marcus_code_*.py tests/test_native_tools.py -q`
- The FULL suite (150 tests, all passing as of this handoff) needs
  Docker infra: `docker compose up -d postgres redis rabbitmq`, then
  `HARNESS_TEST_DATABASE_URL=postgresql+asyncpg://harness:harness@localhost:5432/harness_test HARNESS_REDIS_URL=redis://localhost:6379/0 uv run pytest -q`.
  **Gotcha:** any running harness worker/API steals RabbitMQ messages
  from tests — stop `marcus-api-1`/`marcus-worker-1` containers and any
  local `uvicorn`/`workers.main` processes before the full suite.
- **Do not break the existing 150 tests.** Run the full suite once at
  the end.

## Known sharp edges (learned this session)

- `mcp` SDK connect failures raise `CancelledError` (a BaseException)
  through task groups — already fixed in `harness/mcp/client.py`; if you
  touch async task-group code, catch `BaseException`, not `Exception`.
- Starlette `BaseHTTPMiddleware` corrupts nested anyio cancel scopes —
  irrelevant to the CLI, but don't reintroduce it anywhere.
- `Settings()` is `lru_cache`d via `get_settings()` — in tests,
  construct `Settings(...)` directly with overrides instead of mutating
  env.

## Acceptance (verify before calling it done)

1. `uv run marcus` in a scratch directory: agent answers a question
   about a file it reads, edits a file after y-confirmation showing a
   diff, runs `echo hello` via run_cli after confirmation.
2. Denying a prompt (n) feeds a "user declined" observation back to the
   model and the loop continues gracefully.
3. Path escape attempts (`../outside.txt`) are refused by every fs tool.
4. New unit tests pass with no Docker running; full suite still 100%
   with infra up.
5. Works against the user's Ollama Cloud config (they will supply
   `HARNESS_LLM_API_KEY` at test time).

## Suggested first hour

1. Read `harness/runtime/engine.py` (loop shape), `harness/llm/gateway.py`
   + `types.py` (client contract), `harness/runtime/native_tools.py`
   (tool patterns), `tests/fakes.py` (ScriptedLLMGateway).
2. Scaffold `marcus_code/` + entry point + a hardcoded "hello loop"
   that calls the real gateway once.
3. Build tools + approval callback with unit tests.
4. Wire the loop, then the rich UI last.
