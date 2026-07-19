from dataclasses import dataclass, field
from typing import Any, Literal

import orjson

Role = Literal["system", "user", "assistant", "tool"]
ReasoningEffort = Literal["off", "low", "medium", "high", "auto"]
LLMProvider = Literal["auto", "openai", "openrouter", "ollama", "nvidia", "compatible"]

_PROVIDER_MESSAGE_FIELDS = frozenset({"reasoning", "reasoning_content", "reasoning_details"})


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
    provider_fields: dict[str, Any] = field(default_factory=dict)

    def to_openai(self, *, provider_fields: frozenset[str] | None = None) -> dict[str, Any]:
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
        allowed = _PROVIDER_MESSAGE_FIELDS if provider_fields is None else provider_fields
        for key, value in self.provider_fields.items():
            if key in allowed:
                message[key] = value
        return message


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    source: Literal["provider", "estimated"] = "provider"
    reasoning_tokens: int = 0


@dataclass
class LLMOptions:
    reasoning_effort: ReasoningEffort = "auto"
    thinking_enabled: bool | None = None
    max_completion_tokens: int | None = None
    reasoning_budget_tokens: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: Usage
    model: str
    raw: dict[str, Any]
    provider_fields: dict[str, Any] = field(default_factory=dict)


def _dump_arguments(arguments: dict[str, Any]) -> str:
    return orjson.dumps(arguments).decode()
