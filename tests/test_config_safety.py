import pytest
from pydantic import ValidationError

from harness.config import Settings


def test_production_rejects_default_secrets_and_legacy_auth():
    with pytest.raises(ValidationError, match="unsafe production configuration"):
        Settings(env="production")


def test_production_accepts_explicit_secure_configuration():
    settings = Settings(
        env="production",
        secret_key="a-production-secret-that-is-long-enough",
        llm_api_key="sk-production",
        legacy_auth_enabled=False,
    )

    assert settings.env == "production"


def test_compaction_thresholds_must_be_ordered():
    with pytest.raises(ValidationError, match="target < threshold"):
        Settings(cli_compact_target_percent=90, cli_compact_threshold_percent=80)


def test_server_compaction_threshold_must_be_a_percentage():
    with pytest.raises(ValidationError, match="run_compact_threshold_percent"):
        Settings(run_compact_threshold_percent=100)


@pytest.mark.parametrize("field", ["run_max_tool_calls_per_step", "cli_max_tool_calls_per_step"])
def test_per_step_tool_limits_must_be_positive(field):
    with pytest.raises(ValidationError, match=field):
        Settings(**{field: 0})


@pytest.mark.parametrize("field", ["run_max_argument_repairs", "cli_max_argument_repairs"])
def test_argument_repair_limits_cannot_be_negative(field):
    with pytest.raises(ValidationError, match="max_argument_repairs"):
        Settings(**{field: -1})
