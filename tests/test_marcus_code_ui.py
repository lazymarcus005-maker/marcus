import math
from datetime import datetime
from io import StringIO

import pytest
from prompt_toolkit.document import Document
from rich.console import Console

from marcus_code.ollama_usage import OllamaCloudUsage, UsagePeriod
from marcus_code.todo_tracker import Phase, TodoTracker
from marcus_code.ui import SlashCommandAutoSuggest, TerminalUI, Theme, _history_path


@pytest.mark.asyncio
async def test_slash_command_auto_suggest_filters_and_limits_to_seven():
    suggester = SlashCommandAutoSuggest()
    fake_buffer = None

    async def suggest(text: str) -> str | None:
        doc = Document(text, len(text))
        suggestion = await suggester.get_suggestion_async(fake_buffer, doc)
        return suggestion.text if suggestion else None

    assert await suggest("/") == "  |  ".join(
        [
            "/help",
            "/?",
            "/model",
            "/usage",
            "/steps",
            "/status",
            "/compact",
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


def test_thinking_indicator_stays_inside_working_box():
    ui, stream = _capturing_ui()

    ui.begin_turn()
    ui.start_thinking()
    ui.console.print(ui._working_renderable())
    assert "thinking" in stream.getvalue()

    ui.stop_thinking(1.234)
    ui.console.print(ui._working_renderable())
    assert "thought 1.2s" in stream.getvalue()


def test_working_view_is_one_minimal_box_for_phase_thinking_and_steps():
    ui, stream = _capturing_ui()
    ui.bind_status(
        lambda: {
            "session_started_at": datetime.now(),
            "model": "test-model",
            "mode": "agent",
            "workspace": "workspace",
            "context_tokens": 25,
            "context_limit": 100,
            "total_tokens": 50,
            "tokens_per_second": 10.0,
        }
    )
    ui.begin_turn()
    todo = TodoTracker()
    todo.advance(Phase.implement, "working")
    ui.update_todo(todo)
    ui.print_tool_call("read_file", {"path": "app.py"})
    ui.print_tool_result("read_file", {"path": "app.py", "lines": 20})
    ui.start_thinking()

    ui.console.print(ui._working_renderable())

    output = stream.getvalue()
    assert output.count("┌") == 1
    assert output.count("└") == 1
    assert "ดำเนินการ" in output
    assert "app.py" in output
    assert "thinking" in output
    assert "test-model · ctx 25% · agent" in output
    assert "Session" not in output


def test_tool_call_collapses_when_finished_unsuccessfully():
    ui, stream = _capturing_ui()

    ui.print_tool_call("run_cli", {"command": "curl http://localhost:5234/"})
    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "× stopped · 1 tool(s) · /steps" in output


def test_command_result_collapses_and_steps_expands():
    ui, stream = _capturing_ui()

    ui.print_tool_call("run_cli", {"command": "echo '{\"usd\":100,\"thb\":3550}'"})
    ui.print_tool_result(
        "run_cli",
        {"exit_code": 0, "stdout": '{"usd":100,"thb":3550}', "stderr": ""},
    )
    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "× stopped · 1 tool(s) · /steps" in output

    ui.print_steps()
    expanded = stream.getvalue()
    assert "Command finished with exit code 0" in expanded
    assert '"usd":100' in expanded
    assert '"thb":3550' in expanded


def test_process_results_collapses_and_steps_expands():
    ui, stream = _capturing_ui()

    ui.print_tool_call("start_process", {"command": "node server.js"})
    ui.print_tool_result(
        "start_process",
        {"status": "running", "process_id": "abc123def456", "pid": 42},
    )
    ui.print_tool_call("stop_process", {"process_id": "abc123def456"})
    ui.print_tool_result("stop_process", {"process_id": "abc123def456", "status": "stopped"})
    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "× stopped · 2 tool(s) · /steps" in output

    ui.print_steps()
    expanded = stream.getvalue()
    assert "process abc123def456, PID 42" in expanded
    assert "Service stopped (process abc123def456)" in expanded


def test_final_answer_has_spacing_heading_and_content():
    ui, stream = _capturing_ui()
    ui.print_tool_call("run_cli", {"command": "pytest -q"})
    ui.print_tool_result("run_cli", {"exit_code": 0, "stdout": "1 passed", "stderr": ""})

    ui.print_final_answer("API ทำงานสำเร็จ")

    output = stream.getvalue()
    assert "✓ done · 1 tool(s) · /steps" in output
    assert output.rstrip().endswith("API ทำงานสำเร็จ")


def test_direct_answer_without_tools_has_no_result_heading_or_preamble():
    ui, stream = _capturing_ui()

    ui.print_final_answer("JWT คือมาตรฐานสำหรับส่งข้อมูลแบบมีลายเซ็น")

    output = stream.getvalue()
    assert output.startswith("JWT คือ")
    assert "ผลการทำงาน" not in output
    assert "งานสำเร็จ" not in output


def test_success_collapses_steps_and_steps_command_restores_details():
    ui, stream = _capturing_ui()
    ui.print_tool_call("run_cli", {"command": "pytest -q"})
    ui.print_tool_result("run_cli", {"exit_code": 0, "stdout": "10 passed", "stderr": ""})

    ui.finish_steps(success=True)
    collapsed = stream.getvalue()
    assert "✓ done · 1 tool(s) · /steps" in collapsed

    ui.print_steps()
    expanded = stream.getvalue()
    assert "run_cli(command='pytest -q')" in expanded
    assert "10 passed" in expanded
    assert "Last steps" in expanded


def test_working_box_keeps_latest_lines_visible_when_terminal_is_short():
    ui, stream = _capturing_ui(height=12)
    for index in range(8):
        ui.print_tool_call("read_file", {"path": f"file-{index}.txt"})
        ui.print_tool_result("read_file", {"path": f"file-{index}.txt", "lines": index + 1})

    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "× stopped · 8 tool(s) · /steps" in output

    ui.print_steps()
    expanded = stream.getvalue()
    assert "earlier line(s) hidden; use /steps" in expanded
    assert "file-7.txt" in expanded


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
    assert "gpt-oss:120b         |  Provider" in output
    assert "agent                |  Profile" in output


def test_banner_shows_version_when_provided(tmp_path):
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
    assert "Version" in output and "1.2.3" in output


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
    assert "\u2588" not in text.plain
    assert "\u2591" not in text.plain


def test_history_path_creates_user_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("marcus_code.ui.USER_CONFIG_DIR", tmp_path / ".marcus")
    path = _history_path()
    assert path.parent.exists()
    assert path.name == "history"
