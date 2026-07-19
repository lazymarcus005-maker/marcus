import httpx
import pytest

import marcus_code.state.config as config_module
from marcus_code.state.config import (
    fetch_available_models,
    has_llm_credentials,
    load_project_instructions,
    load_user_config,
    resolve_settings,
    save_user_config,
    validate_base_url,
)


class _FakeResponse:
    def __init__(self, payload, *, status_error=False):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _point_user_config_at(monkeypatch, tmp_path):
    config_dir = tmp_path / "home" / ".marcus"
    monkeypatch.setattr(config_module, "USER_CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "USER_CONFIG_FILE", config_dir / "config.toml")
    return config_dir


def test_load_user_config_returns_empty_dict_when_missing(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)

    assert load_user_config() == {}


def test_validate_base_url_accepts_http_and_removes_trailing_slash():
    assert validate_base_url(" https://api.example.com/v1/// ") == "https://api.example.com/v1"


@pytest.mark.parametrize(
    "url", ["file:///tmp/x", "localhost:1234", "https://user:pass@example.com/v1"]
)
def test_validate_base_url_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_save_and_load_user_config_round_trips(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)

    path = save_user_config(
        api_key="sk-secret", base_url="https://api.openai.com/v1", model="gpt-4o-mini"
    )

    assert path.is_file()
    loaded = load_user_config()
    assert loaded == {
        "api_key": "sk-secret",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    }


def test_save_user_config_escapes_quotes_and_backslashes(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)

    save_user_config(api_key='key"with"quotes', base_url="https://example.com/v1", model="m")

    loaded = load_user_config()
    assert loaded["api_key"] == 'key"with"quotes'


def test_load_user_config_returns_empty_on_malformed_toml(tmp_path, monkeypatch):
    config_dir = _point_user_config_at(monkeypatch, tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text("not [ valid toml", encoding="utf-8")

    assert load_user_config() == {}


def test_resolve_settings_applies_file_config_when_no_env_var(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.delenv("HARNESS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_LLM_MODEL", raising=False)
    save_user_config(
        api_key="sk-from-file", base_url="https://api.openai.com/v1", model="gpt-4o-mini"
    )

    settings = resolve_settings()

    assert settings.llm_api_key == "sk-from-file"
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model == "gpt-4o-mini"


def test_resolve_settings_env_var_wins_over_file(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    save_user_config(
        api_key="sk-from-file", base_url="https://api.openai.com/v1", model="gpt-4o-mini"
    )
    monkeypatch.setenv("HARNESS_LLM_API_KEY", "sk-from-env")

    settings = resolve_settings()

    assert settings.llm_api_key == "sk-from-env"
    # base_url/model still come from the file since only api_key has an env override
    assert settings.llm_base_url == "https://api.openai.com/v1"


def test_resolve_settings_falls_back_to_defaults_with_no_file_and_no_env(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.delenv("HARNESS_LLM_API_KEY", raising=False)

    settings = resolve_settings()

    assert has_llm_credentials(settings) is False


def test_has_llm_credentials_true_once_a_real_key_is_set(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.setenv("HARNESS_LLM_API_KEY", "sk-real")

    settings = resolve_settings()

    assert has_llm_credentials(settings) is True


def test_cli_max_steps_defaults_to_100(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.delenv("HARNESS_CLI_MAX_STEPS", raising=False)

    assert resolve_settings().cli_max_steps == 100


def test_cli_max_steps_can_be_overridden_by_environment(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.setenv("HARNESS_CLI_MAX_STEPS", "150")

    assert resolve_settings().cli_max_steps == 150


def test_fetch_available_models_returns_sorted_unique_ids(monkeypatch):
    captured = {}

    def fake_get(url, *, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(
            {"data": [{"id": "gpt-oss:120b"}, {"id": "deepseek-v3"}, {"id": "gpt-oss:120b"}]}
        )

    monkeypatch.setattr(config_module.httpx, "get", fake_get)

    models = fetch_available_models("https://ollama.com/v1", "sk-key")

    assert models == ["deepseek-v3", "gpt-oss:120b"]
    assert captured["url"] == "https://ollama.com/v1/models"
    assert captured["headers"] == {"Authorization": "Bearer sk-key"}


def test_fetch_available_models_returns_empty_on_http_error(monkeypatch):
    def fake_get(url, *, headers, timeout):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(config_module.httpx, "get", fake_get)

    assert fetch_available_models("https://ollama.com/v1", "sk-key") == []


def test_fetch_available_models_returns_empty_on_unexpected_payload(monkeypatch):
    def fake_get(url, *, headers, timeout):
        return _FakeResponse({"unexpected": "shape"})

    monkeypatch.setattr(config_module.httpx, "get", fake_get)

    assert fetch_available_models("https://ollama.com/v1", "sk-key") == []


def test_load_project_instructions_returns_none_when_missing(tmp_path):
    assert load_project_instructions(tmp_path) is None


def test_load_project_instructions_reads_marcus_md(tmp_path):
    marcus_dir = tmp_path / ".marcus"
    marcus_dir.mkdir()
    (marcus_dir / "MARCUS.md").write_text("Always use tabs, not spaces.", encoding="utf-8")

    result = load_project_instructions(tmp_path)

    assert result == "Always use tabs, not spaces."


def test_load_project_instructions_returns_none_for_blank_file(tmp_path):
    marcus_dir = tmp_path / ".marcus"
    marcus_dir.mkdir()
    (marcus_dir / "MARCUS.md").write_text("   \n\n  ", encoding="utf-8")

    assert load_project_instructions(tmp_path) is None
