from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HARNESS_", extra="ignore")

    env: str = "development"

    database_url: str = "postgresql+asyncpg://harness:harness@localhost:5432/harness"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://harness:harness@localhost:5672/"

    llm_base_url: str = "https://ollama.com/v1"
    llm_api_key: str = "changeme"
    llm_model: str = "gpt-oss:120b"
    llm_timeout_seconds: float = 60.0

    secret_key: str = "changeme-32-bytes-minimum-secret-key!!"

    run_default_max_steps: int = 25
    run_default_max_tool_calls: int = 40
    run_default_token_budget: int = 200_000
    run_default_timeout_seconds: int = 900

    approval_expiry_hours: int = 24
    run_lease_ttl_seconds: int = 60

    tool_result_max_chars: int = 4000

    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    slack_tenant_id: str | None = None
    web_base_url: str = "http://localhost:8000"

    api_key_rate_limit_per_minute: int = 120

    otel_enabled: bool = True
    otel_service_name: str = "harness"
    otel_exporter_otlp_endpoint: str | None = None
    json_logs: bool = True

    scheduler_lease_ttl_seconds: int = 30
    scheduler_poll_seconds: int = 30

    tenant_daily_token_quota_default: int = 2_000_000
    tenant_max_active_runs_default: int = 25

    # Built-in tools (harness/runtime/native_tools.py): fetch_url, read_file,
    # write_file, run_cli. read_file/write_file/run_cli are sandboxed to this
    # directory and cannot escape it.
    tools_fs_root: str = "./data/tools-fs"
    tools_fetch_url_timeout_seconds: float = 15.0
    tools_fetch_url_max_bytes: int = 200_000
    tools_read_file_max_bytes: int = 200_000
    # run_cli is DESTRUCTIVE risk tier — every call requires human approval —
    # but arbitrary shell execution is still meaningful blast radius. Set to
    # false to remove the tool entirely for deployments that don't want it.
    tools_run_cli_enabled: bool = True
    tools_run_cli_timeout_seconds: float = 30.0
    tools_run_cli_max_output_bytes: int = 50_000

    # Marcus CLI session guardrails.
    cli_max_history_messages: int = 100
    cli_max_total_tokens: int | None = None
    cli_history_summary_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
