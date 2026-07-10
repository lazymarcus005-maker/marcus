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
```

Not in this migration — added by later issues:
`approval_requests` (#17), `scheduled_jobs` (#25), `skills` / `skill_revisions`
/ `skill_usage` (#18), `mcp_servers` / `mcp_tools` (#14).

## Conventions

- Every tenant-scoped table carries `tenant_id` directly or transitively via
  `run_id` — set from day one per `decisions.md` Q5, even with a single tenant.
- `agent_runs.version` is the optimistic-locking fencing token described in
  `decisions.md` D5 (gap G1). The repository layer built in #4 must increment
  it on every checkpoint write and reject writes with a stale version.
- `tool_executions.idempotency_key` is written *before* the tool call executes
  (write-ahead), per `decisions.md` D6 (gap G2). Recovery policy is keyed off
  `risk_tier`.

## Local development

```bash
uv run alembic upgrade head
uv run python -m harness.db.seed   # creates a 'default' tenant + admin user
```
