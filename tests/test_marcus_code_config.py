import marcus_code.config as config_module
from marcus_code.config import (
    has_llm_credentials,
    load_project_instructions,
    load_user_config,
    resolve_settings,
    save_user_config,
)


def _point_user_config_at(monkeypatch, tmp_path):
    config_dir = tmp_path / "home" / ".marcus"
    monkeypatch.setattr(config_module, "USER_CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "USER_CONFIG_FILE", config_dir / "config.toml")
    return config_dir


def test_load_user_config_returns_empty_dict_when_missing(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)

    assert load_user_config() == {}


def test_save_and_load_user_config_round_trips(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)

    path = save_user_config(api_key="sk-secret", base_url="https://api.openai.com/v1", model="gpt-4o-mini")

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
    save_user_config(api_key="sk-from-file", base_url="https://api.openai.com/v1", model="gpt-4o-mini")

    settings = resolve_settings()

    assert settings.llm_api_key == "sk-from-file"
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model == "gpt-4o-mini"


def test_resolve_settings_env_var_wins_over_file(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    save_user_config(api_key="sk-from-file", base_url="https://api.openai.com/v1", model="gpt-4o-mini")
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
