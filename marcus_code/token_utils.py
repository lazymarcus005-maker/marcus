"""Token estimation and message importance ranking for MarcusLoop."""

from __future__ import annotations

from typing import Any

import orjson

from harness.llm.types import LLMMessage

# Character-to-token ratios observed empirically for English + code text.
# These are conservative over-estimates so we do not accidentally overflow.
CHARS_PER_TOKEN_CODE = 3.5
CHARS_PER_TOKEN_ENGLISH = 4.0


def estimate_message_tokens(message: LLMMessage) -> int:
    """Estimate token count for a single message more accurately than bytes/4."""
    content = message.content or ""
    # Tool call JSON adds overhead.
    if message.tool_calls:
        content += orjson.dumps(
            [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": orjson.dumps(call.arguments).decode(),
                    },
                }
                for call in message.tool_calls
            ]
        ).decode()
    if message.name:
        content += f"\n[{message.name}]"
    if message.tool_call_id:
        content += f"\n(id: {message.tool_call_id})"

    # Use code ratio for messages with many symbols; otherwise English ratio.
    ratio = CHARS_PER_TOKEN_CODE if _is_mostly_code(content) else CHARS_PER_TOKEN_ENGLISH
    # Add a fixed overhead per message (role + formatting tokens).
    overhead = 4
    return max(1, overhead + int(len(content) / ratio))


def _is_mostly_code(text: str) -> bool:
    if not text:
        return False
    code_indicators = sum(1 for ch in text if ch in "{}[]()=;|&<>!+-*/%:\\\\'\"")
    return code_indicators / max(1, len(text)) > 0.03


def rank_message_importance(messages: list[LLMMessage]) -> list[tuple[int, int]]:
    """Return (index, score) pairs where higher score means more important to keep."""
    scored: list[tuple[int, int]] = []
    for idx, message in enumerate(messages):
        score = 0
        if message.role == "system":
            score += 1000
        elif message.role == "user":
            score += 500
        elif message.role == "assistant":
            score += 300
        elif message.role == "tool":
            score += 100
        # Tool results that contain errors or verification evidence are more important.
        if message.role == "tool" and message.content:
            try:
                data = orjson.loads(message.content)
            except orjson.JSONDecodeError:
                data = {}
            if isinstance(data, dict):
                if data.get("exit_code", 0) != 0 or "error" in data:
                    score += 150
                if data.get("status") == "ok" and "verification" in str(data).lower():
                    score += 100
        scored.append((idx, score))
    return scored


def trim_messages_to_budget(
    messages: list[LLMMessage],
    token_budget: int,
    *,
    preserve_system: bool = True,
    preserve_latest_user: bool = True,
) -> list[LLMMessage]:
    """Trim messages to fit a token budget by dropping lowest-importance first."""
    if not messages:
        return []

    protected: set[int] = set()
    if preserve_system:
        for idx, message in enumerate(messages):
            if message.role == "system":
                protected.add(idx)
    if preserve_latest_user:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].role == "user":
                protected.add(idx)
                break

    ranked = sorted(rank_message_importance(messages), key=lambda item: item[1])
    # Start with all indices, then drop lowest-score non-protected until we fit.
    keep = set(range(len(messages)))
    current_tokens = sum(estimate_message_tokens(m) for m in messages)
    if current_tokens <= token_budget:
        return list(messages)

    for idx, _score in ranked:
        if idx in protected:
            continue
        if idx not in keep:
            continue
        keep.remove(idx)
        current_tokens -= estimate_message_tokens(messages[idx])
        if current_tokens <= token_budget:
            break

    return [messages[idx] for idx in sorted(keep)]


def summarize_tool_result(content: str, max_chars: int = 800) -> str:
    """Compress a verbose tool result into a short summary for history retention."""
    if len(content) <= max_chars:
        return content
    try:
        data = orjson.loads(content)
    except orjson.JSONDecodeError:
        return content[:max_chars] + "... [truncated]"

    if not isinstance(data, dict):
        return content[:max_chars] + "... [truncated]"

    summary: dict[str, Any] = {}
    # Keep high-signal keys.
    for key in ("status", "exit_code", "error", "path", "url", "ready", "process_id", "command"):
        if key in data:
            summary[key] = data[key]
    # Compress lists to counts.
    for key in ("files", "matches", "children", "results", "todos", "processes"):
        if key in data and isinstance(data[key], list):
            summary[key] = f"{len(data[key])} item(s)"
    # Summarize long text fields.
    for key in ("content", "stdout", "stderr", "diff", "text", "snippet"):
        if key in data and isinstance(data[key], str) and len(data[key]) > 120:
            summary[key] = data[key][:120] + "... [truncated]"
        elif key in data:
            summary[key] = data[key]

    json_summary = orjson.dumps(summary).decode()
    if len(json_summary) <= max_chars:
        return json_summary
    return json_summary[:max_chars] + "... [truncated]"
