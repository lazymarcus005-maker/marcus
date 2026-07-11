from typing import Any

import orjson


def truncate_result(value: Any, *, max_chars: int) -> dict[str, Any]:
    """Tool result pipeline's truncate step (issue #16, decisions.md Q14).

    Normalizes any tool result to a dict (subsuming the plain-value coercion
    ToolExecutor used to do inline), then — if its serialized size exceeds
    max_chars — replaces it with a head/tail-preserving digest instead of
    shipping the raw payload back to the LLM. Rule-based only, no LLM
    summarize step in MVP: intentionally dumb and cheap.
    """
    result = value if isinstance(value, dict) else {"value": value}

    serialized = orjson.dumps(result).decode()
    if len(serialized) <= max_chars:
        return result

    half = max_chars // 2
    head = serialized[:half]
    tail = serialized[-half:]
    omitted = len(serialized) - len(head) - len(tail)
    return {
        "_truncated": True,
        "_original_length": len(serialized),
        "content": f"{head}...[truncated {omitted} chars]...{tail}",
    }
