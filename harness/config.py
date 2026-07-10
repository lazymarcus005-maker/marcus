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


@lru_cache
def get_settings() -> Settings:
    return Settings()
