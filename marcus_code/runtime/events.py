"""Agent → UI events.

Pydantic models are used so events can be serialized later for a web UI or
IPC transport without changing the emit sites. For now the event bus fans
out to an in-process renderer synchronously, so the extra validation cost
per event is negligible.

Only one-way notifications flow through the bus. Two-way calls that need
a return value from the user (``confirm_tool_call``, ``prompt_user``) stay
as direct method calls on the UI object — modeling those as
request/response events would just add ceremony.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict


class _Event(BaseModel):
    """Common config: allow arbitrary Python objects (TodoTracker) as payload
    since events don't cross process boundaries yet."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)


class TurnStarted(_Event):
    kind: Literal["turn_started"] = "turn_started"


class TurnFinished(_Event):
    kind: Literal["turn_finished"] = "turn_finished"
    success: bool


class TodoUpdated(_Event):
    kind: Literal["todo_updated"] = "todo_updated"
    tracker: Any


class ThinkingStarted(_Event):
    kind: Literal["thinking_started"] = "thinking_started"


class ThinkingStopped(_Event):
    kind: Literal["thinking_stopped"] = "thinking_stopped"
    elapsed_seconds: float


class StreamStarted(_Event):
    kind: Literal["stream_started"] = "stream_started"


class StreamDelta(_Event):
    kind: Literal["stream_delta"] = "stream_delta"
    text: str


class StreamEnded(_Event):
    kind: Literal["stream_ended"] = "stream_ended"


class AssistantMessage(_Event):
    kind: Literal["assistant_message"] = "assistant_message"
    text: str


class FinalAnswer(_Event):
    kind: Literal["final_answer"] = "final_answer"
    text: str


class ToolCallStarted(_Event):
    kind: Literal["tool_call_started"] = "tool_call_started"
    tool_name: str
    arguments: dict[str, Any]


class ToolCallCompleted(_Event):
    kind: Literal["tool_call_completed"] = "tool_call_completed"
    tool_name: str
    result: dict[str, Any]


class ToolCallFailed(_Event):
    kind: Literal["tool_call_failed"] = "tool_call_failed"
    tool_name: str
    error: str


class ToolCallDeclined(_Event):
    kind: Literal["tool_call_declined"] = "tool_call_declined"
    tool_name: str


class Recovery(_Event):
    kind: Literal["recovery"] = "recovery"
    message: str


class GuardrailStop(_Event):
    kind: Literal["guardrail_stop"] = "guardrail_stop"
    reason: str


class Interrupted(_Event):
    kind: Literal["interrupted"] = "interrupted"


Event = Union[
    TurnStarted,
    TurnFinished,
    TodoUpdated,
    ThinkingStarted,
    ThinkingStopped,
    StreamStarted,
    StreamDelta,
    StreamEnded,
    AssistantMessage,
    FinalAnswer,
    ToolCallStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallDeclined,
    Recovery,
    GuardrailStop,
    Interrupted,
]
