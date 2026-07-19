"""Bridge from agent events to the terminal renderer.

Subscribe an instance of ``TerminalRenderer`` to an ``EventBus`` and the
existing ``TerminalUI`` will receive the same method calls it always did
— the bus only decouples *who* triggers the call, not what the call is.

This lets us later drop in a Textual/web renderer without touching agent
code.
"""

from __future__ import annotations

from marcus_code.runtime.events import (
    AssistantMessage,
    Event,
    FinalAnswer,
    GuardrailStop,
    Interrupted,
    Recovery,
    StreamDelta,
    StreamEnded,
    StreamStarted,
    ThinkingStarted,
    ThinkingStopped,
    TodoUpdated,
    ToolCallCompleted,
    ToolCallDeclined,
    ToolCallFailed,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from marcus_code.ui.console import TerminalUI


class TerminalRenderer:
    """Dispatch events to ``TerminalUI`` methods."""

    def __init__(self, ui: TerminalUI) -> None:
        self._ui = ui

    def handle(self, event: Event) -> None:
        # Straight isinstance dispatch keeps this easy to read and easy for
        # mypy to understand. If the event list grows large enough that
        # this table becomes unwieldy, swap to a dict of {kind: callback}.
        if isinstance(event, TurnStarted):
            self._ui.begin_turn()
        elif isinstance(event, TurnFinished):
            self._ui.finish_steps(success=event.success)
        elif isinstance(event, TodoUpdated):
            self._ui.update_todo(event.tracker)
        elif isinstance(event, ThinkingStarted):
            self._ui.start_thinking()
        elif isinstance(event, ThinkingStopped):
            self._ui.stop_thinking(event.elapsed_seconds)
        elif isinstance(event, StreamStarted):
            self._ui.start_stream()
        elif isinstance(event, StreamDelta):
            self._ui.stream_delta(event.text)
        elif isinstance(event, StreamEnded):
            self._ui.end_stream()
        elif isinstance(event, AssistantMessage):
            self._ui.print_assistant(event.text)
        elif isinstance(event, FinalAnswer):
            self._ui.print_final_answer(event.text)
        elif isinstance(event, ToolCallStarted):
            self._ui.print_tool_call(event.tool_name, event.arguments)
        elif isinstance(event, ToolCallCompleted):
            self._ui.print_tool_result(event.tool_name, event.result)
        elif isinstance(event, ToolCallFailed):
            self._ui.print_tool_error(event.tool_name, event.error)
        elif isinstance(event, ToolCallDeclined):
            self._ui.print_tool_declined(event.tool_name)
        elif isinstance(event, Recovery):
            self._ui.print_recovery(event.message)
        elif isinstance(event, GuardrailStop):
            self._ui.print_guardrail_stop(event.reason)
        elif isinstance(event, Interrupted):
            self._ui.print_interrupted()
