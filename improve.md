# Marcus Code — Improvement Backlog

Working list of gaps found while reviewing `marcus_code/` after the
`/model`, `/usage`, `/config` commands and banner redesign landed. Not
scheduled — pick off items as needed. Priority reflects risk/impact, not
effort.

## Security / guardrails (High priority)

- **`fetch_url` has no SSRF protection.** [marcus_code/tools.py](marcus_code/tools.py)
  `build_fetch_url_tool` is `risk_tier=read_only`, so it runs without
  approval, and accepts any `http(s)://` URL — including
  `http://169.254.169.254/...` (cloud metadata) and `http://localhost/...`.
  Needs a blocklist for loopback/private/link-local IPs (resolve the
  hostname before connecting, not just string-match the URL, to close DNS
  rebinding).
- **No secret-aware handling in `read_file`.** Reading `.env`,
  `*.pem`, `credentials.json`, etc. sends the raw content straight to the
  LLM with no warning or redaction. The new system prompt
  ([marcus_code/prompt.py](marcus_code/prompt.py)) asks the model not to
  echo secrets back, but that's a soft guardrail — a determined or
  confused model can still ignore it. Consider a filename/content-pattern
  check that redacts obvious secrets before they enter tool output.
- **`run_cli` has no command allow/deny list.** Mitigated today by
  `risk_tier=destructive` (approval required every call), but the
  confirmation prompt doesn't flag known-dangerous patterns (`rm -rf`,
  `--force`, `DROP TABLE`, `git push --force`). Surfacing that in the
  approval prompt itself would make the human-in-the-loop check more
  effective.

## Reliability

- **`SessionState.history` has no cap.** [marcus_code/loop.py](marcus_code/loop.py)
  Long sessions grow the message list unbounded and will eventually blow
  past the model's context window with a hard failure. Needs either
  truncation of old turns or a summarization pass.
- **No session-level budget/cost cap.** `/usage` shows spend after the
  fact but nothing stops a runaway loop from burning tokens across many
  turns.
- **No git-awareness in the system prompt.** The agent doesn't know if
  the working directory has uncommitted changes, what branch it's on, etc.
  unless it runs `git status` itself via `run_cli` (which needs approval).
  Injecting a short git summary into the system prompt at startup would
  give it that context for free.

## UX

- **No non-interactive mode.** Everything requires the REPL; there's no
  `marcus -p "task"` one-shot mode for scripting/CI use.
- **No streaming output.** Replies only appear once the full completion
  arrives, which reads as "stuck" on longer generations.
- **`/config edit` doesn't validate input.** A malformed `base_url` isn't
  caught until the next LLM call fails. Basic scheme/format validation at
  edit time would surface the mistake immediately.

## Test coverage

- **`marcus_code/cli.py` (`_amain`/REPL loop) is untested.** Covered so
  far only by manual live runs (scripted stdin against the real global
  install). No automated coverage of the first-time-setup flow or the
  `llm.aclose()` error path.

## Recently addressed (for context)

- `/model`, `/usage`, `/config` slash commands — see [marcus_code/commands.py](marcus_code/commands.py).
- System prompt rewritten with identity, guardrails, and self-description
  — see [marcus_code/prompt.py](marcus_code/prompt.py).
- Banner panel widened 10%, logo margin, left-aligned tagline, cyan
  underline — see [marcus_code/banner.py](marcus_code/banner.py) and
  [marcus_code/ui.py](marcus_code/ui.py).
- `/mcp` intentionally deferred — CLI has zero MCP integration today (only
  the 7 built-in tools); revisit once there's a concrete need.
