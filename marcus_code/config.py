import contextlib
import os
import tomllib
from pathlib import Path

from harness.config import Settings

USER_CONFIG_DIR = Path.home() / ".marcus"
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.toml"
PROJECT_CONFIG_DIR_NAME = ".marcus"
PROJECT_INSTRUCTIONS_FILENAME = "MARCUS.md"

# Env vars that, if already set, must win over ~/.marcus/config.toml — a
# value the user explicitly exported for this shell session shouldn't be
# silently overridden by a file they may have forgotten exists.
_ENV_VAR_BY_FIELD = {
    "llm_api_key": "HARNESS_LLM_API_KEY",
    "llm_base_url": "HARNESS_LLM_BASE_URL",
    "llm_model": "HARNESS_LLM_MODEL",
}


def load_user_config() -> dict:
    """Read ~/.marcus/config.toml, expecting an [llm] table with
    api_key/base_url/model keys. Returns {} if the file doesn't exist or
    can't be parsed — config is optional, env vars always work standalone.
    """
    if not USER_CONFIG_FILE.is_file():
        return {}
    try:
        data = tomllib.loads(USER_CONFIG_FILE.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    return data.get("llm", {})


def save_user_config(*, api_key: str, base_url: str, model: str) -> Path:
    """Write ~/.marcus/config.toml, creating the directory if needed.

    Best-effort chmod 600 — meaningful on POSIX, a no-op on Windows (see
    docs/marcus-code-handoff.md's Windows-11-dev-machine context).
    """
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    content = (
        "[llm]\n"
        f'api_key = "{_toml_escape(api_key)}"\n'
        f'base_url = "{_toml_escape(base_url)}"\n'
        f'model = "{_toml_escape(model)}"\n'
    )
    USER_CONFIG_FILE.write_text(content, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(USER_CONFIG_FILE, 0o600)
    return USER_CONFIG_FILE


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def resolve_settings() -> Settings:
    """Layer ~/.marcus/config.toml under env-var-derived Settings: env vars
    (including .env) win when explicitly set, the user config file fills in
    what's left, and harness.config.Settings' own defaults are the final
    fallback.

    Builds a fresh Settings() rather than harness.config.get_settings() —
    that accessor is process-wide lru_cache'd for the long-running server,
    which would make this stick to whatever env vars were present on the
    first call in the process and ignore anything set afterward (including,
    critically, per-test monkeypatched env vars). A CLI invocation is a
    fresh process each time anyway, so there's no caching benefit to lose.
    """
    base = Settings()
    file_config = load_user_config()
    overrides = {}
    for field, env_var in _ENV_VAR_BY_FIELD.items():
        if env_var in os.environ:
            continue
        file_key = field.removeprefix("llm_")
        value = file_config.get(file_key)
        if value:
            overrides[field] = value
    return base.model_copy(update=overrides) if overrides else base


def has_llm_credentials(settings: Settings) -> bool:
    """True once an API key has been configured from any source (env, .env,
    or ~/.marcus/config.toml) — as opposed to harness.config.Settings'
    built-in placeholder default."""
    return bool(settings.llm_api_key) and settings.llm_api_key != Settings.model_fields["llm_api_key"].default


def load_project_instructions(root: Path) -> str | None:
    """Read <root>/.marcus/MARCUS.md if present — project-specific system
    prompt additions, analogous to CLAUDE.md. Unlike ~/.marcus/config.toml
    (a secret, never committed), this file is meant to be checked into the
    project's own repo and shared with the team.
    """
    path = root / PROJECT_CONFIG_DIR_NAME / PROJECT_INSTRUCTIONS_FILENAME
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
