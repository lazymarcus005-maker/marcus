import httpx
import pytest

from marcus_code.runtime.ollama_usage import (
    OllamaCloudUsageClient,
    find_installed_browsers,
    interactive_browser_args,
    is_ollama_cloud,
    load_cached_ollama_email,
    parse_ollama_usage,
    save_cached_ollama_email,
)


def test_interactive_login_uses_real_chrome_without_automation_sandbox_flags(tmp_path):
    args = interactive_browser_args(tmp_path / "profile", 9222)

    assert "--remote-debugging-port=9222" in args
    assert not any("no-sandbox" in argument for argument in args)
    assert not any("enable-automation" in argument for argument in args)


def test_browser_discovery_prefers_chrome_from_path(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "marcus_code.runtime.ollama_usage.shutil.which",
        lambda command: str(chrome) if command == "chrome" else None,
    )

    assert find_installed_browsers()[0] == chrome


def test_detects_ollama_cloud_base_urls():
    assert is_ollama_cloud("https://ollama.com/v1") is True
    assert is_ollama_cloud("https://api.ollama.com/v1") is True
    assert is_ollama_cloud("http://localhost:11434/v1") is False


def test_parses_session_and_weekly_usage():
    usage = parse_ollama_usage(
        "Session usage\n14.3% used\nResets in 2 hours\n"
        "Weekly usage\n14.5% used\nResets in 10 hours\nuser@example.com\n"
    )

    assert usage is not None
    assert usage.session is not None
    assert usage.session.percent == 14.3
    assert usage.session.resets_in == "2 hours"
    assert usage.weekly is not None
    assert usage.weekly.percent == 14.5
    assert usage.weekly.resets_in == "10 hours"
    assert usage.email == "user@example.com"


def test_parser_returns_none_when_settings_structure_is_unknown():
    assert parse_ollama_usage("Account settings without usage data") is None


def test_cached_profile_email_round_trip(tmp_path):
    cache_file = tmp_path / "profile.json"

    save_cached_ollama_email("user@example.com", cache_file)

    assert load_cached_ollama_email(cache_file) == "user@example.com"


def test_saved_login_is_detected_from_storage_state(tmp_path):
    state_file = tmp_path / "storage-state.json"
    client = OllamaCloudUsageClient(tmp_path / "browser-profile", state_file)
    assert client.has_profile is False

    state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    assert client.has_profile is True


def test_logout_removes_saved_session_cache_and_browser_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("marcus_code.runtime.ollama_usage.Path.home", lambda: tmp_path)
    marcus_home = tmp_path / ".marcus"
    profile = marcus_home / "browser-profile"
    profile.mkdir(parents=True)
    (profile / "cookie-db").write_text("secret", encoding="utf-8")
    state = marcus_home / "state.json"
    cache = marcus_home / "cache.json"
    state.write_text("{}", encoding="utf-8")
    cache.write_text("{}", encoding="utf-8")
    client = OllamaCloudUsageClient(profile, state)

    removed = client.logout(cache_file=cache)

    assert removed == 3
    assert not profile.exists()
    assert not state.exists()
    assert not cache.exists()


@pytest.mark.asyncio
async def test_plain_usage_fetches_with_saved_cookies_without_browser(tmp_path, monkeypatch):
    state_file = tmp_path / "storage-state.json"
    state_file.write_text(
        '{"cookies":[{"name":"session","value":"secret","domain":".ollama.com",'
        '"path":"/"}],"origins":[]}',
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie") == "session=secret"
        return httpx.Response(
            200,
            text="Session usage 10% used Resets in 1 hour\nWeekly usage 20% used",
            request=request,
        )

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "marcus_code.runtime.ollama_usage.httpx.AsyncClient",
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler), cookies=kwargs.get("cookies")
        ),
    )
    usage_client = OllamaCloudUsageClient(tmp_path / "profile", state_file)

    usage = await usage_client.fetch(interactive=False)

    assert usage.session is not None and usage.session.percent == 10
