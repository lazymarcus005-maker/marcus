# Ops Runbook

## Health

- `GET /healthz`: process liveness.
- `GET /readyz`: PostgreSQL, Redis, and RabbitMQ connectivity.
- `GET /metrics`: Prometheus metrics.
- Web UI: `/app`.

## Observability

Docker Compose includes:

- Jaeger UI: `http://localhost:16686`
- Prometheus: `http://localhost:9090`

Set `HARNESS_OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP collector endpoint to
export traces. JSON logs are enabled with `HARNESS_JSON_LOGS=true`.

Key Prometheus metrics:

- `harness_http_requests_total`
- `harness_runs_started_total`
- `harness_runs_completed_total`
- `harness_llm_tokens_total`
- `harness_tool_calls_total`

## Scheduler

Run `python -m harness.scheduler` or use the `scheduler` Compose service.
Only one instance fires jobs at a time via Redis key `scheduler:leader`.

Useful checks:

```bash
redis-cli GET scheduler:leader
curl -H "X-API-Key: $KEY" http://localhost:8000/v1/scheduled-jobs
```

## Recovery

- Stale running runs are recovered by the worker reaper loop.
- Expired approvals transition pending approval requests to `expired` and the
  run to `timed_out`.
- RabbitMQ dead-letter queue: `agent.runs.dlq`.

## Quotas

Run creation is blocked with `429` when:

- Active non-terminal runs exceed `max_active_runs`.
- Daily token usage exceeds `daily_token_quota`.

Defaults come from `HARNESS_TENANT_*`; tenant-specific overrides live in
`tenant_quotas`.
