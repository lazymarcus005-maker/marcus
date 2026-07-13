import math
from datetime import datetime
from io import StringIO

import pytest
from rich.console import Console

from marcus_code.ollama_usage import OllamaCloudUsage, UsagePeriod
from marcus_code.ui import _APPROVAL_PROMPT, TerminalUI


def test_approval_prompt_uses_pastel_orange_and_red_sections():
    assert "[#F2B880]Allow this tool call? (y)es / (n)o /" in _APPROVAL_PROMPT
    assert "[#F28B82](a)lways for this session" in _APPROVAL_PROMPT


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


def test_tool_call_shows_action_and_exact_invocation():
    ui, stream = _capturing_ui()

    ui.print_tool_call("run_cli", {"command": "curl http://localhost:5234/"})
    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "Run command" in output
    assert "run_cli(command='curl http://localhost:5234/')" in output


def test_command_result_shows_exit_code_and_stdout_evidence():
    ui, stream = _capturing_ui()

    ui.print_tool_result(
        "run_cli",
        {"exit_code": 0, "stdout": '{"usd":100,"thb":3550}', "stderr": ""},
    )
    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "Command finished with exit code 0" in output
    assert '"usd":100' in output
    assert '"thb":3550' in output


def test_process_results_show_identifiers_and_status():
    ui, stream = _capturing_ui()

    ui.print_tool_result(
        "start_process",
        {"status": "running", "process_id": "abc123def456", "pid": 42},
    )
    ui.print_tool_result("stop_process", {"process_id": "abc123def456", "status": "stopped"})
    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "process abc123def456, PID 42" in output
    assert "Service stopped (process abc123def456)" in output


def test_final_answer_has_spacing_heading_and_content():
    ui, stream = _capturing_ui()
    ui.print_tool_call("run_cli", {"command": "pytest -q"})
    ui.print_tool_result("run_cli", {"exit_code": 0, "stdout": "1 passed", "stderr": ""})

    ui.print_final_answer("API ทำงานสำเร็จ")

    output = stream.getvalue()
    assert "\nผลการทำงาน\n\n" in output
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
    assert "งานสำเร็จ · 1 ขั้นตอน" in collapsed
    assert "/steps" in collapsed

    ui.print_steps()
    expanded = stream.getvalue()
    assert "run_cli(command='pytest -q')" in expanded
    assert "10 passed" in expanded


def test_working_box_keeps_latest_lines_visible_when_terminal_is_short():
    ui, stream = _capturing_ui(height=12)
    for index in range(8):
        ui.print_tool_call("read_file", {"path": f"file-{index}.txt"})
        ui.print_tool_result("read_file", {"path": f"file-{index}.txt", "lines": index + 1})

    ui.finish_steps(success=False)

    output = stream.getvalue()
    assert "earlier line(s) hidden; use /steps" in output
    assert "file-7.txt" in output


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
    ui._prompt_session = session

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
    ui._prompt_session = _InterruptingPromptSession()

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
    ui._prompt_session = _SequencePromptSession()

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
    ui._prompt_session = session

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
