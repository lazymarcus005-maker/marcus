# Feature Work: Reasoning Effort Layer

## Goal

Add first-class reasoning effort controls to Marcus Code so users can tune the
cost, latency, and answer quality trade-off without changing models manually.

## Implemented

- Added `ReasoningEffort` values: `off`, `low`, `medium`, `high`, `auto`.
- Added `LLMOptions` for per-call model behavior:
  - `reasoning_effort`
  - `thinking_enabled`
  - `max_completion_tokens`
  - `extra_body`
- Added settings:
  - `HARNESS_LLM_REASONING_EFFORT`
  - `HARNESS_LLM_MAX_COMPLETION_TOKENS`
- Added `/effort` slash command:
  - `/effort`
  - `/effort off`
  - `/effort low`
  - `/effort medium`
  - `/effort high`
  - `/effort auto`
- Persisted effort settings to `~/.marcus/config.toml`.
- Preserved effort settings when `/model`, `/config edit`, and first-time setup save config.
- Displayed effort in terminal status, config output, command help, and TUI status.
- Passed `max_completion_tokens` into OpenAI-compatible payloads.
- Added generic system hints for non-auto effort modes.

## Current Behavior

`auto` leaves the model behavior unchanged.

`off`, `low`, `medium`, and `high` add a system-level hint before the LLM call.
`off` also marks `thinking_enabled=False` in `LLMOptions`, ready for provider
adapters in a later phase.

`max_completion_tokens`, when configured, is sent as `max_completion_tokens` in
the chat completions payload.

## Verified

- Focused ruff checks passed for touched files.
- Focused tests passed: `134 passed`.
- Full test suite passed: `311 passed, 123 skipped`.
- CLI help still renders for:
  - `uv run marcus --help`
  - `uv run marcus tui --help`

## Not Implemented Yet

Provider-specific reasoning adapters are intentionally not part of this phase.
The current implementation avoids sending unsupported provider-specific fields
by default.

## Next Work

1. Add provider adapter layer.
   - OpenAI-compatible/gpt-oss: map effort to supported request fields or system prompt.
   - Qwen-style models: map `off` to disabled thinking template behavior.
   - Nemotron-style models: support `enable_thinking`, `medium_effort`, and hard budgets.

2. Add `/effort max-tokens` control.
   - Example: `/effort max-tokens 4096`
   - Example: `/effort max-tokens default`

3. Add auto effort router.
   - Direct questions: `low` or `off`
   - Code reading/explanation: `medium`
   - Multi-file edits, CI, release, debugging: `high`
   - Low remaining token budget: downgrade effort automatically

4. Add metrics by effort.
   - tokens
   - latency
   - tool calls
   - retries
   - guardrail stops

5. Add provider compatibility tests.
   - Ensure unsupported fields are not sent to strict OpenAI-compatible providers.
   - Ensure adapter-specific payloads are generated only when selected.

