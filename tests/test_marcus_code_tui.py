"""Focused tests for the TUI event renderer + prompter.

The Textual app itself needs a live terminal to boot, so we don't spin it
up here — we stub the pieces the ``TuiRenderer`` and ``TuiPrompter`` talk
to and assert that events flow through the same shape a live Textual app
would see.
"""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")  # skip cleanly when the extra isn't installed

from marcus_code.runtime.events import (
    AssistantMessage,
    FinalAnswer,
    GuardrailStop,
    Recovery,
    ThinkingStarted,
    ThinkingStopped,
    ToolCallCompleted,
    ToolCallDeclined,
    ToolCallFailed,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from marcus_code.runtime.todo_tracker import Phase, TodoTracker
from marcus_code.runtime.events import TodoUpdated
from marcus_code.ui.tui import TuiPrompter, TuiRenderer


class _FakeApp:
    def __init__(self) -> None:
        self.log_lines: list[str] = []
        self.thinking_states: list[bool] = []

    def write_log(self, text: str) -> None:
        self.log_lines.append(text)

    def set_thinking(self, active: bool) -> None:
        self.thinking_states.append(active)


def test_renderer_writes_tool_lifecycle_in_order():
    app = _FakeApp()
    renderer = TuiRenderer(app)  # type: ignore[arg-type]

    renderer.handle(TurnStarted())
    renderer.handle(ThinkingStarted())
    renderer.handle(ThinkingStopped(elapsed_seconds=1.2))
    renderer.handle(ToolCallStarted(tool_name="read_file", arguments={"path": "app.py"}))
    renderer.handle(ToolCallCompleted(tool_name="read_file", result={"path": "app.py", "lines": 3}))
    renderer.handle(TurnFinished(success=True))

    assert app.thinking_states == [True, False]
    # First non-empty log line is the "thought" summary from stop_thinking.
    assert any("thought 1.2s" in line for line in app.log_lines)
    # Tool call line uses the numbered arrow prefix.
    assert any("▸ 1. read_file" in line for line in app.log_lines)
    # Result line uses the check mark.
    assert any("Read app.py (3 lines)" in line for line in app.log_lines)
    # Summary shows the tool count.
    assert any("done · 1 tool(s)" in line for line in app.log_lines)


def test_renderer_prints_phase_breadcrumb_only_on_transition():
    app = _FakeApp()
    renderer = TuiRenderer(app)  # type: ignore[arg-type]
    renderer.handle(TurnStarted())

    tracker = TodoTracker()
    tracker.advance(Phase.analyze, "looking")
    renderer.handle(TodoUpdated(tracker=tracker))
    # Same phase again — must not repeat the breadcrumb.
    renderer.handle(TodoUpdated(tracker=tracker))

    tracker.advance(Phase.implement, "coding")
    renderer.handle(TodoUpdated(tracker=tracker))

    breadcrumbs = [line for line in app.log_lines if line.startswith("[dim]·")]
    assert len(breadcrumbs) == 2
    assert "วิเคราะห์" in breadcrumbs[0]
    assert "ดำเนินการ" in breadcrumbs[1]


def test_renderer_reports_failures_and_declines_distinctly():
    app = _FakeApp()
    renderer = TuiRenderer(app)  # type: ignore[arg-type]
    renderer.handle(TurnStarted())
    renderer.handle(ToolCallStarted(tool_name="run_cli", arguments={"command": "boom"}))
    renderer.handle(ToolCallFailed(tool_name="run_cli", error="exit 1"))
    renderer.handle(ToolCallDeclined(tool_name="run_cli"))
    renderer.handle(TurnFinished(success=False))

    assert any("[red]×[/red] run_cli: exit 1" in line for line in app.log_lines)
    assert any("run_cli declined" in line for line in app.log_lines)
    assert any("stopped · 1 tool(s)" in line for line in app.log_lines)


def test_renderer_renders_assistant_messages_and_recovery():
    app = _FakeApp()
    renderer = TuiRenderer(app)  # type: ignore[arg-type]
    renderer.handle(AssistantMessage(text="thinking about this"))
    renderer.handle(Recovery(message="retrying"))
    renderer.handle(FinalAnswer(text="ทำเสร็จแล้ว"))
    renderer.handle(GuardrailStop(reason="ctx exceeded"))

    joined = "\n".join(app.log_lines)
    assert "thinking about this" in joined
    assert "↻ retrying" in joined
    assert "[bold]ทำเสร็จแล้ว[/bold]" in joined
    assert "guardrail · ctx exceeded" in joined


def test_prompter_exposes_hasattr_stubs_for_loop_gates():
    class _App:
        pass

    prompter = TuiPrompter(_App())  # type: ignore[arg-type]

    # These attributes exist as callable no-ops so agent.py's hasattr()
    # gates fire and events actually get emitted.
    for name in TuiPrompter._STUB_METHODS:
        method = getattr(prompter, name)
        assert callable(method)
        assert method("anything", extra=1) is None

    # Streaming stubs are deliberately absent so the loop stays on the
    # non-streaming code path (much easier to render into a RichLog).
    assert not hasattr(prompter, "start_stream")
    assert not hasattr(prompter, "stream_delta")
    assert not hasattr(prompter, "end_stream")


def test_prompter_confirm_tool_call_pushes_screen_and_returns_decision():
    import asyncio
    from unittest.mock import AsyncMock

    class _App:
        push_screen_wait = AsyncMock(return_value="always")

    prompter = TuiPrompter(_App())  # type: ignore[arg-type]

    class _FakeTool:
        name = "run_cli"

    result = asyncio.run(prompter.confirm_tool_call(_FakeTool(), {"command": "ls"}))

    assert result == "always"
    _App.push_screen_wait.assert_awaited_once()
