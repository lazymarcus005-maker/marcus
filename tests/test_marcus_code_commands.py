import pytest

import marcus_code.state.config as config_module
from harness.config import Settings
from harness.llm.types import LLMMessage
from marcus_code.cli.commands import CommandContext, dispatch
from marcus_code.state.config import save_user_config
from marcus_code.runtime.agent import MarcusLoop
from marcus_code.runtime.ollama_usage import OllamaCloudUsage, UsagePeriod
from tests.fakes import ScriptedLLMGateway, text_response


class _FakeUI:
    def __init__(self, *, config_edit_result=None):
        self.info: list[str] = []
        self.errors: list[str] = []
        self.help_shown = False
        self.usage_calls: list = []
        self.config_shown: list = []
        self._config_edit_result = config_edit_result
        self.config_edit_calls: list[dict] = []
        self.ollama_usage = []

    def print_info(self, message):
        self.info.append(message)

    def print_error(self, message):
        self.errors.append(message)

    def print_help(self):
        self.help_shown = True

    def print_usage(self, stats, *, session_started_at, max_total_tokens=None):
        self.usage_calls.append((stats, session_started_at))

    def print_config(self, settings):
        self.config_shown.append(settings)

    def run_config_edit(self, *, current_base_url, current_model, has_existing_key):
        self.config_edit_calls.append(
            {
                "current_base_url": current_base_url,
                "current_model": current_model,
                "has_existing_key": has_existing_key,
            }
        )
        return self._config_edit_result

    def print_ollama_cloud_usage(self, usage):
        self.ollama_usage.append(usage)

    def clear_screen(self):
        self.screen_cleared = True

    def set_theme(self, name):
        self.theme = name

    def prompt_multiline(self):
        return getattr(self, "multiline_input", None)

    def print_assistant(self, text):
        self.info.append(text)

    def print_final_answer(self, text):
        self.info.append(text)

    def begin_turn(self):
        pass


def _point_user_config_at(monkeypatch, tmp_path):
    config_dir = tmp_path / "home" / ".marcus"
    monkeypatch.setattr(config_module, "USER_CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "USER_CONFIG_FILE", config_dir / "config.toml")
    return config_dir


class _ClosableScriptedLLMGateway(ScriptedLLMGateway):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _make_ctx(ui=None, *, settings=None, model=None):
    llm = _ClosableScriptedLLMGateway([text_response("ok")])
    loop = MarcusLoop(llm, [], ui or _FakeUI(), model=model)
    settings = settings or Settings(
        llm_api_key="sk-real", llm_base_url="https://api.example.com/v1", llm_model="m1"
    )
    return CommandContext(ui=ui or loop.ui, loop=loop, settings=settings)


@pytest.mark.asyncio
async def test_dispatch_exit_commands_signal_stop():
    ctx = _make_ctx()

    assert await dispatch(ctx, "/exit") is False
    assert await dispatch(ctx, "/quit") is False


@pytest.mark.asyncio
async def test_dispatch_unknown_command_reports_error():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    result = await dispatch(ctx, "/nope")

    assert result is True
    assert "unknown command" in ui.errors[0]


@pytest.mark.asyncio
async def test_dispatch_help_shows_help():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/help")

    assert ui.help_shown is True


@pytest.mark.asyncio
async def test_model_command_with_no_args_shows_current_model():
    ui = _FakeUI()
    ctx = _make_ctx(ui, model="gpt-4o-mini")

    await dispatch(ctx, "/model")

    assert ui.info == ["Current model: gpt-4o-mini"]


@pytest.mark.asyncio
async def test_model_command_switches_model_for_session():
    ui = _FakeUI()
    ctx = _make_ctx(ui, model="gpt-4o-mini")

    await dispatch(ctx, "/model llama-3.1-70b")

    assert ctx.loop.model == "llama-3.1-70b"
    assert "llama-3.1-70b" in ui.info[0]


@pytest.mark.asyncio
async def test_usage_command_reports_loop_usage_stats():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/usage")

    assert len(ui.usage_calls) == 1
    stats, started_at = ui.usage_calls[0]
    assert stats is ctx.loop.usage
    assert started_at is ctx.loop.started_at


@pytest.mark.asyncio
async def test_usage_command_fetches_ollama_cloud_usage(monkeypatch):
    expected = OllamaCloudUsage(
        session=UsagePeriod(14.3, "2 hours"),
        weekly=UsagePeriod(14.5, "10 hours"),
    )

    class _Client:
        has_profile = True

        async def fetch(self, *, interactive=False):
            assert interactive is False
            return expected

    monkeypatch.setattr("marcus_code.cli.commands.OllamaCloudUsageClient", _Client)
    ui = _FakeUI()
    settings = Settings(
        llm_api_key="sk-real",
        llm_base_url="https://ollama.com/v1",
        llm_model="model",
    )
    ctx = _make_ctx(ui, settings=settings)

    await dispatch(ctx, "/usage")

    assert ui.ollama_usage == [expected]


@pytest.mark.asyncio
async def test_usage_logout_clears_saved_ollama_session(monkeypatch):
    class _Client:
        def logout(self):
            return 3

    monkeypatch.setattr("marcus_code.cli.commands.OllamaCloudUsageClient", _Client)
    ui = _FakeUI()
    settings = Settings(
        llm_api_key="sk-real",
        llm_base_url="https://ollama.com/v1",
        llm_model="model",
    )
    ctx = _make_ctx(ui, settings=settings)

    await dispatch(ctx, "/usage logout")

    assert any("login data cleared" in message for message in ui.info)


@pytest.mark.asyncio
async def test_config_command_with_no_args_shows_current_settings():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/config")

    assert ui.config_shown == [ctx.settings]


@pytest.mark.asyncio
async def test_config_edit_cancelled_leaves_settings_unchanged():
    ui = _FakeUI(config_edit_result=None)
    ctx = _make_ctx(ui)
    original_settings = ctx.settings

    await dispatch(ctx, "/config edit")

    assert ctx.settings is original_settings


@pytest.mark.asyncio
async def test_config_edit_saves_new_values_and_rebuilds_llm(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.delenv("HARNESS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("HARNESS_LLM_MODEL", raising=False)
    save_user_config(api_key="sk-old", base_url="https://old.example.com/v1", model="old-model")

    ui = _FakeUI(config_edit_result=("sk-new", "https://new.example.com/v1", "new-model"))
    settings = Settings(
        llm_api_key="sk-old", llm_base_url="https://old.example.com/v1", llm_model="old-model"
    )
    ctx = _make_ctx(ui, settings=settings)
    old_llm = ctx.loop.llm

    await dispatch(ctx, "/config edit")

    assert ctx.settings.llm_api_key == "sk-new"
    assert ctx.settings.llm_base_url == "https://new.example.com/v1"
    assert ctx.settings.llm_model == "new-model"
    assert ctx.loop.model == "new-model"
    assert ctx.loop.llm is not old_llm
    assert "updated" in ui.info[0].lower()


@pytest.mark.asyncio
async def test_config_edit_blank_api_key_keeps_existing_key(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.delenv("HARNESS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("HARNESS_LLM_MODEL", raising=False)

    ui = _FakeUI(config_edit_result=(None, "https://new.example.com/v1", "new-model"))
    settings = Settings(
        llm_api_key="sk-keep-me", llm_base_url="https://old.example.com/v1", llm_model="old-model"
    )
    ctx = _make_ctx(ui, settings=settings)

    await dispatch(ctx, "/config edit")

    assert ctx.settings.llm_api_key == "sk-keep-me"
    assert ctx.settings.llm_base_url == "https://new.example.com/v1"


@pytest.mark.asyncio
async def test_config_edit_blank_api_key_with_no_existing_key_errors(tmp_path, monkeypatch):
    _point_user_config_at(monkeypatch, tmp_path)
    monkeypatch.delenv("HARNESS_LLM_API_KEY", raising=False)

    ui = _FakeUI(config_edit_result=(None, "https://new.example.com/v1", "new-model"))
    settings = Settings(llm_api_key=Settings.model_fields["llm_api_key"].default)
    ctx = _make_ctx(ui, settings=settings)

    await dispatch(ctx, "/config edit")

    assert any("api key" in err.lower() for err in ui.errors)


@pytest.mark.asyncio
async def test_config_unknown_action_reports_error():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/config bogus")

    assert "unknown /config action" in ui.errors[0]


@pytest.mark.asyncio
async def test_mode_command_shows_and_switches_mode():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/mode")
    await dispatch(ctx, "/mode auto")

    assert ui.info[0].startswith("Current mode: agent")
    assert all(mode in ui.info[0] for mode in ("ask", "agent", "auto", "yolo"))
    assert ctx.loop.mode.value == "auto"
    assert "auto" in ui.info[1]
    assert "high-risk" in ui.info[1]


@pytest.mark.asyncio
async def test_mode_command_rejects_unknown_mode():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/mode reckless")

    assert "unknown mode" in ui.errors[0]


@pytest.mark.asyncio
async def test_clear_command_clears_context_and_all_clears_approvals():
    ui = _FakeUI()
    ctx = _make_ctx(ui)
    ctx.loop.state.history.append(LLMMessage(role="user", content="old context"))
    ctx.loop.state.always_allowed.add("run_cli")

    await dispatch(ctx, "/clear")
    assert not any(message.role == "user" for message in ctx.loop.state.history)
    assert ctx.loop.state.always_allowed == {"run_cli"}
    assert ui.screen_cleared is True

    await dispatch(ctx, "/clear --all")
    assert ctx.loop.state.always_allowed == set()
    assert ui.screen_cleared is True


@pytest.mark.asyncio
async def test_compact_command_reports_before_and_after():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/compact")

    assert "estimated tokens" in ui.info[-1]


@pytest.mark.asyncio
async def test_theme_command_shows_and_switches_theme():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/theme")
    await dispatch(ctx, "/theme no-color")

    assert "Current theme" in ui.info[0]
    assert "no-color" in ui.info[0]
    assert ui.info[1] == "Theme switched to 'no-color'."


@pytest.mark.asyncio
async def test_theme_command_rejects_unknown_theme():
    ui = _FakeUI()
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/theme neon")

    assert "unknown theme" in ui.errors[0]


@pytest.mark.asyncio
async def test_edit_command_submits_multiline_input_to_loop():
    ui = _FakeUI()
    ui.multiline_input = "line one\nline two"
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/edit")

    assert "submitted" in ui.info[0]


@pytest.mark.asyncio
async def test_edit_command_cancels_when_input_empty():
    ui = _FakeUI()
    ui.multiline_input = ""
    ctx = _make_ctx(ui)

    await dispatch(ctx, "/edit")

    assert "cancelled" in ui.info[0]
