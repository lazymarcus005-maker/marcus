"""Unit tests for the runtime event bus + terminal renderer bridge.

These lock in the wiring so that a broken renderer, a mistyped event
name, or a subscriber that raises can't silently stop the agent loop from
notifying the UI.
"""

from __future__ import annotations

from marcus_code.runtime.event_bus import EventBus
from marcus_code.runtime.events import (
    AssistantMessage,
    FinalAnswer,
    GuardrailStop,
    Recovery,
    ThinkingStarted,
    ThinkingStopped,
    ToolCallCompleted,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from marcus_code.ui.renderer import TerminalRenderer


class _RecordingUI:
    """Minimal stand-in for TerminalUI that records every method call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs) -> None:
            self.calls.append((name, args, kwargs))

        return _record


def test_event_bus_fans_out_to_all_subscribers_in_order():
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(lambda event: received.append(f"a:{event.kind}"))
    bus.subscribe(lambda event: received.append(f"b:{event.kind}"))

    bus.emit(TurnStarted())
    bus.emit(ThinkingStarted())

    assert received == [
        "a:turn_started",
        "b:turn_started",
        "a:thinking_started",
        "b:thinking_started",
    ]


def test_subscriber_exception_does_not_break_other_subscribers():
    bus = EventBus()

    def _broken(_event):
        raise RuntimeError("boom")

    seen = []
    bus.subscribe(_broken)
    bus.subscribe(seen.append)

    bus.emit(TurnStarted())

    assert len(seen) == 1
    assert seen[0].kind == "turn_started"


def test_terminal_renderer_dispatches_events_to_matching_ui_methods():
    ui = _RecordingUI()
    renderer = TerminalRenderer(ui)  # type: ignore[arg-type]

    renderer.handle(TurnStarted())
    renderer.handle(ThinkingStarted())
    renderer.handle(ThinkingStopped(elapsed_seconds=1.5))
    renderer.handle(ToolCallStarted(tool_name="read_file", arguments={"path": "x.py"}))
    renderer.handle(
        ToolCallCompleted(tool_name="read_file", result={"path": "x.py", "lines": 3})
    )
    renderer.handle(AssistantMessage(text="hi"))
    renderer.handle(FinalAnswer(text="done"))
    renderer.handle(Recovery(message="retrying"))
    renderer.handle(GuardrailStop(reason="ctx exceeded"))
    renderer.handle(TurnFinished(success=True))

    names = [name for name, _args, _kwargs in ui.calls]
    assert names == [
        "begin_turn",
        "start_thinking",
        "stop_thinking",
        "print_tool_call",
        "print_tool_result",
        "print_assistant",
        "print_final_answer",
        "print_recovery",
        "print_guardrail_stop",
        "finish_steps",
    ]
    # Spot-check payload propagation.
    assert ui.calls[2][1] == (1.5,)
    assert ui.calls[3][1] == ("read_file", {"path": "x.py"})
    assert ui.calls[9][2] == {"success": True}


def test_bus_and_renderer_wired_together_call_the_ui():
    ui = _RecordingUI()
    bus = EventBus()
    bus.subscribe(TerminalRenderer(ui).handle)  # type: ignore[arg-type]

    bus.emit(ToolCallStarted(tool_name="grep", arguments={"pattern": "foo"}))
    bus.emit(TurnFinished(success=False))

    names = [name for name, _args, _kwargs in ui.calls]
    assert names == ["print_tool_call", "finish_steps"]
