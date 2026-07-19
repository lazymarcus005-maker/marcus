import math
from datetime import datetime
from io import StringIO

import pytest
from prompt_toolkit.document import Document
from rich.console import Console

from marcus_code.runtime.ollama_usage import OllamaCloudUsage, UsagePeriod
from marcus_code.runtime.todo_tracker import Phase, TodoTracker
from marcus_code.ui.console import SlashCommandAutoSuggest, TerminalUI, Theme, _history_path


@pytest.mark.asyncio
async def test_slash_command_auto_suggest_filters_and_limits_to_seven():
    suggester = SlashCommandAutoSuggest()
    fake_buffer = None

    async def suggest(text: str) -> str | None:
        doc = Document(text, len(text))
        suggestion = await suggester.get_suggestion_async(fake_buffer, doc)
        return suggestion.text if suggestion else None

    # /steps was retired when the working panel was replaced with an
    # append-only timeline — the completer list must reflect that.
    assert await suggest("/") == "  |  ".join(
        [
            "/help",
            "/?",
            "/model",
            "/usage",
            "/status",
            "/compact",
            "/retry",
        ]
    )
    assert await suggest("/m") == "  |  ".join(
        [
            "/model — Show or switch the active model",
            "/mode — Show or switch mode (ask, agent, auto, yolo)",
        ]
    )
    # With many matches we show just the command names to keep the line short.
    assert await suggest("/c") == "  |  ".join(["/compact", "/continue", "/clear", "/config"])
    assert await suggest("/exit") == "/exit — Quit Marcus Code"
    assert await suggest("/zzz") is None
    assert await suggest("hello") is None


def test_approval_prompt_uses_pastel_orange_and_red_sections():
    ui = TerminalUI()
    prompt = ui._approval_prompt
    assert "[#F2B880]Allow this tool call? (y)es / (n)o /" in prompt
    assert "[#F28B82](a)lways for this session" in prompt


def test_clear_approval_prompt_erases_every_wrapped_terminal_line(monkeypatch):
    ui, stream = _capturing_ui()
    monkeypatch.setattr(type(ui.console), "is_terminal", property(lambda self: True))
    monkeypatch.setattr(
        type(ui.console), "size", property(lambda self: type("Size", (), {"width": 30})())
    )

    ui._clear_approval_prompt("always")

    plain_length = len("Allow this tool call? (y)es / (n)o / (a)lways for this session: ")
    expected_lines = math.ceil((plain_length + len("always")) / 30)
    assert stream.getvalue() == "\x1b[1A\x1b[2K" * expected_lines


def _capturing_ui(*, height: int | None = None) -> tuple[TerminalUI, StringIO]:
    stream = StringIO()
    ui = TerminalUI()
    ui.console = Console(
        file=stream, force_terminal=False, color_system=None, width=120, height=height
    )
    return ui, stream


def test_thinking_indicator_emits_a_thought_summary_line():
    # Append-only surface: start_thinking is transient (spinner on TTYs only,
    # no-op in tests) and stop_thinking prints one compact "thought Xs" line.
    ui, stream = _capturing_ui()
    ui.begin_turn()
    ui.start_thinking()
    ui.stop_thinking(1.234)

    output = stream.getvalue()
    assert "thought 1.2s" in output
    # No live-refreshed panel means no box characters.
    assert "┌" not in output and "╭" not in output


def test_tool_call_and_result_render_inline_without_a_box():
    ui, stream = _capturing_ui()
    ui.begin_turn()
    todo = TodoTracker()
    todo.advance(Phase.implement, "working")
    ui.update_todo(todo)
    ui.print_tool_call("read_file", {"path": "app.py"})
    ui.print_tool_result("read_file", {"path": "app.py", "lines": 20})

    output = stream.getvalue()
    # Phase breadcrumb + tool line + result line — three inline rows,
    # no bordered panel around them.
    assert "Implement" in output
    assert "1. Read file" in output
    assert "app.py" in output
    assert "Read app.py (20 lines)" in output
    assert "┌" not in output and "╭" not in output


def test_finish_steps_prints_single_summary_line_on_failure():
    ui, stream = _capturing_ui()
    ui.begin_turn()
    ui.print_tool_call("run_cli", {"command": "curl http://localhost:5234/"})
    ui.finish_steps(success=False)

    output = stream.getvalue()
    # Terminal scrollback shows every tool call already; the summary line
    # no longer hints at a /steps recap since that command was removed.
    assert "× stopped · 1 tool(s)" in output
    assert "/steps" not in output


def test_finish_steps_summary_reports_tool_count_on_success():
    ui, stream = _capturing_ui()
    ui.begin_turn()
    ui.print_tool_call("run_cli", {"command": 'echo hi'})
    ui.print_tool_result(
        "run_cli",
        {"exit_code": 0, "stdout": "hi", "stderr": ""},
    )
    ui.finish_steps(success=True)

    output = stream.getvalue()
    assert "✓ done · 1 tool(s)" in output


def test_final_answer_follows_steps_with_blank_line():
    ui, stream = _capturing_ui()
    ui.begin_turn()
    ui.print_tool_call("run_cli", {"command": "pytest -q"})
    ui.print_tool_result("run_cli", {"exit_code": 0, "stdout": "1 passed", "stderr": ""})

    ui.print_final_answer("API ทำงานสำเร็จ")

    output = stream.getvalue()
    assert "✓ done · 1 tool(s)" in output
    assert output.rstrip().endswith("API ทำงานสำเร็จ")


def test_direct_answer_without_tools_has_no_result_heading_or_preamble():
    ui, stream = _capturing_ui()

    ui.print_final_answer("JWT คือมาตรฐานสำหรับส่งข้อมูลแบบมีลายเซ็น")

    output = stream.getvalue()
    assert output.startswith("JWT คือ")
    assert "done" not in output
    assert "stopped" not in output


def test_status_bar_shows_retained_context_usage_and_session_details():
    ui, stream = _capturing_ui()
    ui.bind_status(
        lambda: {
            "session_started_at": datetime.now(),
            "model": "test-model",
            "mode": "auto",
            "workspace": "D:\\workspace",
            "context_tokens": 16_400,
            "context_limit": 32_000,
            "total_tokens": 84_700,
            "tokens_per_second": 120.0,
        }
    )

    ui.print_status()

    output = stream.getvalue()
    assert "Context" in output
    assert "~16.4k/32.0k" in output
    assert "Used 84.7k tok" in output
    assert "120.0 tok/s" in output
    assert "Mode auto" in output
    assert "D:\\workspace" in output


def test_status_renderable_can_be_shown_without_a_live_input_footer():
    ui, _stream = _capturing_ui()
    ui.bind_status(
        lambda: {
            "session_started_at": datetime.now(),
            "model": "model",
            "mode": "agent",
            "workspace": "workspace",
            "context_tokens": 1,
            "context_limit": 100,
            "total_tokens": 1,
            "tokens_per_second": 1.0,
        }
    )

    status = ui._status_renderable()

    assert "Session" in status.plain


def test_prompt_toolkit_bottom_toolbar_contains_live_status():
    ui, _stream = _capturing_ui()
    ui.bind_status(
        lambda: {
            "session_started_at": datetime.now(),
            "model": "model",
            "mode": "auto",
            "workspace": "workspace",
            "context_tokens": 100,
            "context_limit": 1000,
            "total_tokens": 500,
            "tokens_per_second": 12.0,
        }
    )

    toolbar = ui._bottom_toolbar()
    text = "".join(value for _style, value in toolbar)

    assert text.startswith("\nSession")
    assert "Context" in text
    assert "Mode auto" in text
    assert "Workspace workspace" in text

    toolbar_style = ui._prompt_style.get_attrs_for_style_str("class:bottom-toolbar")
    text_style = ui._prompt_style.get_attrs_for_style_str("class:bottom-toolbar.text")
    assert toolbar_style.reverse is False
    assert text_style.reverse is False
    assert toolbar_style.bgcolor is not None
    assert toolbar_style.bgcolor == "1f1f1f"


@pytest.mark.asyncio
async def test_prompt_user_uses_async_prompt_session():
    class _FakePromptSession:
        def __init__(self):
            self.calls = []

        async def prompt_async(self, message, **kwargs):
            self.calls.append((message, kwargs))
            return "hello"

    ui, _stream = _capturing_ui()
    session = _FakePromptSession()
    ui._prompt_session = session  # type: ignore[assignment]

    result = await ui.prompt_user()

    assert result == "hello"
    assert session.calls[0][0] == [("class:prompt", "AGENT > ")]
    assert session.calls[0][1]["refresh_interval"] == 1
    assert session.calls[0][1]["style"] is ui._prompt_style


@pytest.mark.asyncio
async def test_prompt_user_falls_back_for_non_tty(monkeypatch):
    ui, _stream = _capturing_ui()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "/exit")

    result = await ui.prompt_user()

    assert result == "/exit"


@pytest.mark.asyncio
async def test_three_consecutive_ctrl_c_presses_exit():
    class _InterruptingPromptSession:
        async def prompt_async(self, *args, **kwargs):
            raise KeyboardInterrupt

    ui, stream = _capturing_ui()
    ui._prompt_session = _InterruptingPromptSession()  # type: ignore[assignment]

    assert await ui.prompt_user() == ""
    assert await ui.prompt_user() == ""
    assert await ui.prompt_user() is None

    output = stream.getvalue()
    assert "2 more times" in output
    assert "1 more time" in output
    assert "exiting Marcus" in output


@pytest.mark.asyncio
async def test_normal_input_resets_ctrl_c_counter():
    class _SequencePromptSession:
        def __init__(self):
            self.results = iter([KeyboardInterrupt(), "hello", KeyboardInterrupt()])

        async def prompt_async(self, *args, **kwargs):
            result = next(self.results)
            if isinstance(result, BaseException):
                raise result
            return result

    ui, _stream = _capturing_ui()
    ui._prompt_session = _SequencePromptSession()  # type: ignore[assignment]

    assert await ui.prompt_user() == ""
    assert await ui.prompt_user() == "hello"
    assert await ui.prompt_user() == ""
    assert ui._interrupt_count == 1


@pytest.mark.asyncio
async def test_ui_close_cancels_prompt_background_tasks():
    class _App:
        def __init__(self):
            self.closed = False

        async def cancel_and_wait_for_background_tasks(self):
            self.closed = True

    class _Session:
        def __init__(self):
            self.app = _App()

    ui, _stream = _capturing_ui()
    session = _Session()
    ui._prompt_session = session  # type: ignore[assignment]

    await ui.aclose()

    assert session.app.closed is True


def test_ollama_cloud_usage_renders_progress_bars():
    ui, stream = _capturing_ui()
    ui.print_ollama_cloud_usage(
        OllamaCloudUsage(
            session=UsagePeriod(14.3, "2 hours"),
            weekly=UsagePeriod(14.5, "10 hours"),
        )
    )

    output = stream.getvalue()
    assert "Ollama Cloud Usage" in output
    assert "Session" in output and "14.3%" in output and "2 hours" in output
    assert "Weekly" in output and "14.5%" in output and "10 hours" in output


def test_banner_shows_provider_next_to_model_and_profile_email(tmp_path):
    ui, stream = _capturing_ui()

    ui.print_banner(
        tmp_path,
        model="gpt-oss:120b",
        provider_url="https://ollama.com/v1",
        profile_email="user@example.com",
        mode="agent",
        session_name="session",
    )

    output = stream.getvalue()
    assert "Model" in output and "gpt-oss:120b" in output
    assert "Provider" in output and "https://ollama.com/v1" in output
    assert "Profile" in output and "user@example.com" in output
    assert "Workspace  " in output
    assert "Model      " in output
    assert "Provider   " in output
    assert "Session    " in output


def test_banner_shows_version_in_panel_title_when_provided(tmp_path):
    ui, stream = _capturing_ui()

    ui.print_banner(
        tmp_path,
        model="gpt-oss:120b",
        provider_url="https://ollama.com/v1",
        profile_email="user@example.com",
        mode="agent",
        session_name="session",
        marcus_version="1.2.3",
    )

    output = stream.getvalue()
    assert "Marcus Code (V1.2.3)" in output
    assert "Version" not in output


def test_theme_dataclass_exposes_named_styles():
    theme = Theme.dark()
    assert theme.info == "cyan"
    assert theme.error == "red"
    assert theme.success == "green"
    assert theme.warning == "yellow"
    assert theme.accent == "bold cyan"
    assert theme.muted == "dim"


def test_no_color_theme_replaces_unicode_bar_with_ascii():
    theme = Theme.no_color()
    assert theme.bar_fill == "#"
    assert theme.bar_empty == "-"
    assert theme.success_glyph == "*"


def test_terminal_ui_defaults_to_dark_theme():
    ui = TerminalUI()
    assert ui.theme == Theme.dark()
    assert ui.theme.info == "cyan"


def test_terminal_ui_no_color_flag_uses_no_color_theme():
    ui = TerminalUI(no_color=True)
    assert ui._no_color is True
    assert ui.theme.bar_fill == "#"
    assert ui.console.no_color is True


def test_terminal_ui_detects_no_color_env_var(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    ui = TerminalUI()
    assert ui._no_color is True
    assert ui.theme.bar_fill == "#"


def test_set_theme_switches_named_styles():
    ui = TerminalUI()
    ui.set_theme("light")
    assert ui.theme == Theme.light()
    assert ui.theme.prompt == "bold #0066cc"

    ui.set_theme("high-contrast")
    assert ui.theme == Theme.high_contrast()
    assert ui.theme.info == "white on black"

    ui.set_theme("no-color")
    assert ui.theme == Theme.no_color()
    assert ui._no_color is True


def test_status_renderable_uses_ascii_bars_in_no_color_mode():
    ui, _stream = _capturing_ui()
    ui.set_theme("no-color")
    ui.bind_status(
        lambda: {
            "session_started_at": datetime.now(),
            "model": "model",
            "mode": "agent",
            "workspace": "workspace",
            "context_tokens": 10,
            "context_limit": 100,
            "total_tokens": 1,
            "tokens_per_second": 1.0,
        }
    )
    text = ui._status_renderable()
    assert "[##------------------]" in text.plain
    assert "█" not in text.plain
    assert "░" not in text.plain


def test_history_path_creates_user_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("marcus_code.ui.console.USER_CONFIG_DIR", tmp_path / ".marcus")
    path = _history_path()
    assert path.parent.exists()
    assert path.name == "history"


def test_run_config_edit_defaults_base_url_to_ollama_on_enter(monkeypatch):
    ui, _stream = _capturing_ui()
    # Blank base URL -> Ollama default; then a fresh key.
    inputs = iter(["", "sk-ollama"])
    monkeypatch.setattr(ui.console, "input", lambda *a, **k: next(inputs))
    captured = {}

    def fake_select_model(base_url, api_key, current_model):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return "gpt-oss:120b"

    monkeypatch.setattr(ui, "_select_model", fake_select_model)

    result = ui.run_config_edit(
        current_base_url="https://api.example.com/v1",
        current_model="llama-3.1-70b",
        has_existing_key=True,
        current_api_key="sk-old",
    )

    assert result == ("sk-ollama", "https://ollama.com/v1", "gpt-oss:120b")
    assert captured["base_url"] == "https://ollama.com/v1"
    # The freshly entered key is what the model catalog is fetched with.
    assert captured["api_key"] == "sk-ollama"


def test_run_config_edit_drops_old_key_when_switching_provider(monkeypatch):
    ui, _stream = _capturing_ui()
    # Blank base URL -> Ollama (different host from current); blank key.
    inputs = iter(["", ""])
    monkeypatch.setattr(ui.console, "input", lambda *a, **k: next(inputs))
    captured = {}

    def fake_select_model(base_url, api_key, current_model):
        captured["api_key"] = api_key
        return current_model

    monkeypatch.setattr(ui, "_select_model", fake_select_model)

    ui.run_config_edit(
        current_base_url="https://api.example.com/v1",
        current_model="m0",
        has_existing_key=True,
        current_api_key="sk-old",
    )

    # The api.example.com key must not be reused against the Ollama endpoint.
    assert captured["api_key"] == ""


class _FakeLoopHostilePrompt:
    """Mimics InquirerPy/prompt_toolkit: execute() refuses to run when the
    calling thread already has a running asyncio event loop."""

    def __init__(self, choice: str):
        self._choice = choice

    def execute(self) -> str:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._choice
        raise RuntimeError("Application.run() cannot be called from a running event loop")


def _menu_capable_ui(monkeypatch, prompt):
    """A UI whose TTY checks pass and whose InquirerPy menu is `prompt`."""
    import sys as sys_module

    from InquirerPy import inquirer

    ui, stream = _capturing_ui()
    monkeypatch.setattr(type(ui.console), "is_terminal", property(lambda self: True))
    monkeypatch.setattr(sys_module.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys_module.stdout, "isatty", lambda: True)
    monkeypatch.setattr(inquirer, "select", lambda **kwargs: prompt)
    return ui, stream


@pytest.mark.asyncio
async def test_select_model_menu_works_inside_running_event_loop(monkeypatch):
    # /config edit runs from an async command handler, so the menu is invoked
    # while the REPL's asyncio loop is running. InquirerPy's execute() starts
    # its own prompt_toolkit loop, which cannot nest inside a running loop —
    # the menu must therefore run on a worker thread, not silently fall back
    # to the manual text prompt.
    ui, _stream = _menu_capable_ui(monkeypatch, _FakeLoopHostilePrompt("gpt-oss:120b"))

    choice = ui._select_model_from_menu(["gpt-oss:120b", "llama-3.1-70b"], "llama-3.1-70b")

    assert choice == "gpt-oss:120b"


def test_select_model_menu_reports_reason_when_menu_fails(monkeypatch):
    # A real menu failure must say why instead of silently downgrading to the
    # text prompt (a swallowed exception hid the event-loop bug for a release).
    class _BrokenPrompt:
        def execute(self):
            raise OSError("console handle is invalid")

    ui, stream = _menu_capable_ui(monkeypatch, _BrokenPrompt())

    choice = ui._select_model_from_menu(["m1", "m2"], "m1")

    assert choice is None
    assert "console handle is invalid" in stream.getvalue()
