from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from harness.llm.types import LLMProvider


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HARNESS_", extra="ignore")

    env: str = "development"

    database_url: str = "postgresql+asyncpg://harness:harness@localhost:5432/harness"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://harness:harness@localhost:5672/"

    llm_base_url: str = "https://ollama.com/v1"
    llm_api_key: str = "changeme"
    llm_model: str = "gpt-oss:120b"
    llm_provider: LLMProvider = "auto"
    llm_reasoning_effort: Literal["off", "low", "medium", "high", "auto"] = "auto"
    llm_max_completion_tokens: int | None = None
    llm_reasoning_budget_tokens: int | None = None
    llm_timeout_seconds: float = 60.0

    secret_key: str = "changeme-32-bytes-minimum-secret-key!!"

    run_default_max_steps: int = 25
    run_default_max_tool_calls: int = 40
    run_default_token_budget: int = 200_000
    run_default_timeout_seconds: int = 900
    run_context_window_tokens: int = 131_072
    run_compact_threshold_percent: int = 70
    run_max_tool_calls_per_step: int = 1
    run_max_argument_repairs: int = 1

    approval_expiry_hours: int = 24
    run_lease_ttl_seconds: int = 60

    tool_result_max_chars: int = 4000

    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    slack_tenant_id: str | None = None
    web_base_url: str = "http://localhost:8000"

    api_key_rate_limit_per_minute: int = 120
    legacy_auth_enabled: bool = True

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
    cli_max_steps: int = 100
    cli_context_window_tokens: int = 131_072
    cli_compact_threshold_percent: int = 85
    cli_compact_target_percent: int = 60
    cli_max_total_tokens: int | None = None
    cli_history_summary_enabled: bool = True
    cli_default_mode: str = "agent"
    cli_llm_recovery_timeout_seconds: float = 90.0
    cli_max_tool_calls_per_step: int = 3
    cli_max_argument_repairs: int = 1

    @model_validator(mode="after")
    def validate_safety_and_budgets(self) -> "Settings":
        if not 0 < self.cli_compact_target_percent < self.cli_compact_threshold_percent < 100:
            raise ValueError("CLI compact percentages must satisfy 0 < target < threshold < 100")
        positive_fields = {
            "run_default_max_steps": self.run_default_max_steps,
            "run_default_max_tool_calls": self.run_default_max_tool_calls,
            "run_default_token_budget": self.run_default_token_budget,
            "run_default_timeout_seconds": self.run_default_timeout_seconds,
            "run_context_window_tokens": self.run_context_window_tokens,
            "run_max_tool_calls_per_step": self.run_max_tool_calls_per_step,
            "run_max_argument_repairs": self.run_max_argument_repairs,
            "cli_max_steps": self.cli_max_steps,
            "cli_context_window_tokens": self.cli_context_window_tokens,
            "cli_llm_recovery_timeout_seconds": self.cli_llm_recovery_timeout_seconds,
            "cli_max_tool_calls_per_step": self.cli_max_tool_calls_per_step,
        }
        invalid = [name for name, value in positive_fields.items() if value <= 0]
        if invalid:
            raise ValueError(f"settings must be positive: {', '.join(invalid)}")
        if self.llm_max_completion_tokens is not None and self.llm_max_completion_tokens <= 0:
            raise ValueError("llm_max_completion_tokens must be positive when set")
        if self.llm_reasoning_budget_tokens is not None and self.llm_reasoning_budget_tokens <= 0:
            raise ValueError("llm_reasoning_budget_tokens must be positive when set")
        if self.cli_max_argument_repairs < 0 or self.run_max_argument_repairs < 0:
            raise ValueError("max_argument_repairs must be zero or greater")
        if not 0 < self.run_compact_threshold_percent < 100:
            raise ValueError("run_compact_threshold_percent must be between 1 and 99")

        if self.env.lower() in {"production", "prod"}:
            errors = []
            if (
                self.secret_key == "changeme-32-bytes-minimum-secret-key!!"
                or len(self.secret_key) < 32
            ):
                errors.append("HARNESS_SECRET_KEY must be a non-default value of 32+ characters")
            if self.llm_api_key in {"", "changeme"}:
                errors.append("HARNESS_LLM_API_KEY must not be empty or the default placeholder")
            if self.legacy_auth_enabled:
                errors.append("HARNESS_LEGACY_AUTH_ENABLED must be false")
            if errors:
                raise ValueError("unsafe production configuration: " + "; ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
