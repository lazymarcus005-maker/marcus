from dataclasses import dataclass, field
from typing import Any, Literal

import orjson

Role = Literal["system", "user", "assistant", "tool"]
ReasoningEffort = Literal["off", "low", "medium", "high", "auto"]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMMessage:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            message["content"] = self.content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": _dump_arguments(call.arguments)},
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            message["name"] = self.name
        return message


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    source: Literal["provider", "estimated"] = "provider"


@dataclass
class LLMOptions:
    reasoning_effort: ReasoningEffort = "auto"
    thinking_enabled: bool | None = None
    max_completion_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: Usage
    model: str
    raw: dict[str, Any]


def _dump_arguments(arguments: dict[str, Any]) -> str:
    return orjson.dumps(arguments).decode()
