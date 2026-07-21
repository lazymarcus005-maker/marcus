# Feature Work: Reasoning Effort Layer

## Goal

Give Marcus first-class control over inference effort, latency, and token cost
without coupling the agent runtime to one provider's request format.

## Completed

### Phase 1: Core API

- Added `ReasoningEffort`: `off`, `low`, `medium`, `high`, `auto`.
- Added `LLMOptions` fields for effort, thinking enablement, completion limits,
  hard reasoning budgets, and advanced request fields.
- Added reasoning-token accounting from
  `usage.completion_tokens_details.reasoning_tokens`.

### Phase 2: Config and CLI

- Added `HARNESS_LLM_REASONING_EFFORT`.
- Added `HARNESS_LLM_MAX_COMPLETION_TOKENS`.
- Added `HARNESS_LLM_REASONING_BUDGET_TOKENS`.
- Added `HARNESS_LLM_PROVIDER` for explicit adapter selection behind proxies.
- Added `/effort <off|low|medium|high|auto>`.
- Added `/effort budget <tokens|default>`.
- Added `/effort max-tokens <tokens|default>`.
- Persisted controls in `~/.marcus/config.toml` and preserved them across model
  changes, config edits, and first-time setup.

### Phase 3: Provider Adapters

- Added provider detection and explicit override for:
  - OpenAI
  - Ollama and Ollama Cloud
  - OpenRouter
  - NVIDIA NIM / Nemotron
  - strict OpenAI-compatible endpoints
- Mapped output limits to `max_completion_tokens` or `max_tokens` per provider.
- Mapped OpenAI reasoning models to `reasoning_effort` only when the model family
  supports it.
- Mapped Ollama effort to `reasoning_effort`; GPT-OSS degrades `off` to `low`
  because its reasoning trace cannot be disabled.
- Mapped OpenRouter effort to `reasoning.effort` and hard budgets to
  `reasoning.max_tokens`. A hard budget takes precedence because OpenRouter
  treats those controls as alternatives.
- Mapped Nemotron controls to `chat_template_kwargs.enable_thinking`,
  `low_effort`, and model-specific budget fields:
  - `reasoning_budget`
  - `thinking_token_budget`
  - `max_thinking_tokens`
  - `nvext.max_thinking_tokens`
- Added one-time graceful fallback when a 400/422 response rejects generated
  reasoning controls. The unsupported result is cached per provider, model, and
  effort shape so one rejected level does not disable every level.
- Protected core request fields from `extra_body` overrides.
- Preserved `reasoning`, `reasoning_content`, and `reasoning_details` across tool
  turns, including persisted server runs, while filtering them when the provider
  changes.
- Added compatibility tests for adapter selection, payload shape, hard budgets,
  streaming and non-streaming fallback, fallback caching, and reasoning
  continuation.

### Phase 4: Auto Effort Router

- Added `marcus_code/runtime/effort_router.py`. When effort is `auto` the CLI
  loop resolves a concrete level per turn from the task contract:
  - Direct questions map to `low`.
  - Repository reading and explanation map to `medium`.
  - Changes and operations (multi-file edits, CI, releases, debugging) map to
    `high`.
- Added a graceful downgrade: when the remaining session token budget falls
  below `LOW_BUDGET_RATIO` (20%) the routed level steps down once, with `low`
  as the floor. No downgrade applies when no session budget is configured.
- The router runs per LLM call, so effort tracks the budget as it depletes
  within a long turn, including the planning call.
- An explicit effort level (`off`/`low`/`medium`/`high`) bypasses the router
  entirely; the persisted setting stays `auto` so status still shows `auto`.
- Added unit tests for the router mapping and budget downgrade, plus loop
  integration tests for direct/change routing, budget step-down, and explicit
  override.

## Behavior Notes

- `auto` now resolves to a concrete level per turn via the effort router, so it
  adds the same reasoning hint and provider field as an explicit level. Only an
  unroutable case (no router in a non-CLI caller) leaves `auto` as a no-op.
- A generic system hint remains in place for explicit effort values. It provides
  a useful fallback for models that do not expose native controls.
- Provider-generated reasoning traces are retained for protocol continuity but
  are not rendered as the final answer.
- `extra_body` remains an advanced escape hatch for model-specific fields, but it
  cannot replace `model`, `messages`, `tools`, streaming controls, or temperature.
- NVIDIA budget field selection is model-family based. New NIM model families can
  still be configured through `extra_body` until added to the adapter table.

## Next Work

1. Add effort observability.
   - requested and effective effort
   - reasoning tokens
   - latency
   - tool calls
   - fallback count
   - guardrail stops

2. Add provider capability discovery where APIs expose model metadata.
   - OpenRouter model reasoning capabilities
   - mandatory reasoning models
   - supported effort levels
   - hard-budget support

3. Add opt-in live smoke tests using provider credentials. Unit tests remain
   deterministic and do not make billable requests.
