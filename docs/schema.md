# Schema v1

Managed by Alembic (`migrations/`). Source of truth: `harness/db/models.py`.

```text
tenants
├── id (uuid, pk)
├── name (unique)
└── created_at

users
├── id (uuid, pk)
├── tenant_id (fk -> tenants)
├── display_name
├── role (admin | member)
├── slack_user_id (nullable, unique per tenant)
└── created_at

agent_runs
├── id (uuid, pk)
├── tenant_id (fk -> tenants)
├── created_by_user_id (fk -> users, nullable)
├── status (pending | running | waiting_user_input | waiting_approval |
│           completed | failed | cancelled | timed_out)
├── version (int, optimistic-locking fencing token — every checkpoint write
│            must match the version it read)
├── goal, channel, channel_metadata (jsonb)
├── current_step, max_steps, max_tool_calls, token_budget, timeout_seconds
├── tokens_used, tool_calls_used
├── active_skill_revision_id (nullable — skills land in a later migration)
├── active_tool_names (jsonb list[str] — MCP tools unlocked via load_tool for
│                       progressive disclosure, see decisions.md / issue #15)
├── lease_owner, lease_expires_at (crash-recovery lease, see decisions.md D5)
├── final_result (jsonb, nullable), error, cancel_requested
└── created_at, updated_at

agent_messages
├── id (uuid, pk)
├── run_id (fk -> agent_runs)
├── role (user | assistant | system | tool)
├── content
└── created_at

agent_steps
├── id (uuid, pk)
├── run_id (fk -> agent_runs)
├── step_no (unique per run)
├── type (llm_call | tool_result | summary)
├── payload (jsonb), token_usage (jsonb)
└── created_at

tool_executions
├── id (uuid, pk)
├── run_id (fk -> agent_runs)
├── step_no, call_index
├── idempotency_key (unique — "{run_id}:{step_no}:{call_index}"; written
│                    before the tool call executes, see decisions.md D6)
├── tool_name, mcp_server_id (nullable), risk_tier, idempotent
├── args (jsonb), status (started | succeeded | failed | unknown)
├── result (jsonb, nullable), error
└── started_at, finished_at

usage_records
├── id (uuid, pk)
├── tenant_id (fk -> tenants)
├── run_id (fk -> agent_runs, nullable)
├── model, prompt_tokens, completion_tokens, total_tokens
└── created_at

mcp_servers
├── id (uuid, pk)
├── tenant_id (fk -> tenants)
├── name (unique per tenant — also the Level-1 "domain" for progressive
│         tool disclosure, see decisions.md / issue #15)
├── base_url, auth_header_name (nullable)
├── auth_header_value_encrypted (bytes, nullable — Fernet, see
│                                 harness/mcp/crypto.py, decisions.md Q16)
├── default_risk_tier, enabled
├── health_status (unknown | healthy | unhealthy), last_health_checked_at,
│                  last_error
└── created_at, updated_at

mcp_tools
├── id (uuid, pk)
├── mcp_server_id (fk -> mcp_servers, unique with name)
├── name, description, parameters (jsonb schema)
├── risk_tier (seeded from the server's default_risk_tier at discovery time,
│              independently overridable per tool afterward)
├── enabled (set false, not deleted, when a server no longer reports it)
└── discovered_at

approval_requests
├── id (uuid, pk)
├── tenant_id (fk -> tenants)
├── run_id (fk -> agent_runs)
├── step_no, call_index (unique with run_id — same natural key shape as
│                         tool_executions.idempotency_key)
├── tool_name, risk_tier, args (jsonb)
├── status (pending | approved | rejected | expired)
├── reason, decided_by_user_id (fk -> users, nullable)
├── requested_at, decided_at (nullable), expires_at
```

Not in this migration — added by later issues:
`scheduled_jobs` (#25), `skills` / `skill_revisions` / `skill_usage` (#18).

## Conventions

- Every tenant-scoped table carries `tenant_id` directly or transitively via
  `run_id` — set from day one per `decisions.md` Q5, even with a single tenant.
- `agent_runs.version` is the optimistic-locking fencing token described in
  `decisions.md` D5 (gap G1). The repository layer built in #4 must increment
  it on every checkpoint write and reject writes with a stale version.
- `tool_executions.idempotency_key` is written *before* the tool call executes
  (write-ahead), per `decisions.md` D6 (gap G2). Recovery policy is keyed off
  `risk_tier`.
- `approval_requests` gates `sensitive_write`/`destructive` tool calls
  (`harness.runtime.guardrails.requires_approval`) *before* any
  `tool_executions` row for that call exists — see issue #17. A call is only
  write-ahead-inserted once its approval is `approved`.

## Local development

```bash
uv run alembic upgrade head
uv run python -m harness.db.seed   # creates a 'default' tenant + admin user
```
