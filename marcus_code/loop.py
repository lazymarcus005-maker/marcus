import time
from dataclasses import dataclass, field
from datetime import datetime

import orjson

from harness.llm.gateway import LLMError, LLMGateway
from harness.llm.types import LLMMessage, ToolCall, Usage
from harness.runtime.guardrails import REPEATED_CALL_WINDOW, requires_approval
from harness.runtime.result_pipeline import truncate_result
from harness.runtime.tools import Tool
from marcus_code.ui import TerminalUI

DEFAULT_MAX_STEPS = 25
DEFAULT_RESULT_MAX_CHARS = 4000
DEFAULT_MAX_HISTORY_MESSAGES = 100


@dataclass
class SessionState:
    """Everything that persists across turns within one REPL session (in
    memory only for Phase 1 — see docs/marcus-code-handoff.md)."""

    history: list[LLMMessage] = field(default_factory=list)
    always_allowed: set[str] = field(default_factory=set)


@dataclass
class UsageStats:
    """Cumulative token/timing totals across every LLM call made in this
    session — backs the /usage command."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    elapsed_seconds: float = 0.0

    def record(self, usage: Usage, duration: float) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.llm_calls += 1
        self.elapsed_seconds += duration

    @property
    def tokens_per_second(self) -> float:
        return self.total_tokens / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0


class MarcusLoop:
    """In-process ReAct loop: LLM call -> tool calls (with approval) -> feed
    results back -> repeat, until the model replies with plain text (that's
    the "waiting for the next user message" signal — there's no DB run
    state to checkpoint, so no separate 'finish' tool like the server engine
    uses; see harness/runtime/engine.py for the run-checkpointed cousin of
    this loop).
    """

    def __init__(
        self,
        llm: LLMGateway,
        tools: list[Tool],
        ui: TerminalUI,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        result_max_chars: int = DEFAULT_RESULT_MAX_CHARS,
        max_history_messages: int = DEFAULT_MAX_HISTORY_MESSAGES,
        max_total_tokens: int | None = None,
        history_summary_enabled: bool = True,
    ) -> None:
        self.llm = llm
        self.tools_by_name = {t.name: t for t in tools}
        self.tool_specs = [t.to_spec() for t in tools]
        self.ui = ui
        self.model = model
        self.max_steps = max_steps
        self.result_max_chars = result_max_chars
        self.max_history_messages = max_history_messages
        self.max_total_tokens = max_total_tokens
        self.history_summary_enabled = history_summary_enabled
        self.state = SessionState()
        self.usage = UsageStats()
        self.started_at = datetime.now()
        if system_prompt:
            self.state.history.append(LLMMessage(role="system", content=system_prompt))

    async def run_turn(self, user_input: str) -> None:
        self.state.history.append(LLMMessage(role="user", content=user_input))
        recent_calls: list[tuple[str, dict]] = []

        for _ in range(self.max_steps):
            self._trim_history()
            if self.max_total_tokens is not None and self.usage.total_tokens >= self.max_total_tokens:
                self.ui.print_guardrail_stop(f"session token budget exceeded ({self.max_total_tokens})")
                return
            try:
                start = time.perf_counter()
                if hasattr(self.llm, "complete_stream") and hasattr(self.ui, "print_assistant_delta"):
                    response = await self.llm.complete_stream(
                        self.state.history, tools=self.tool_specs, model=self.model,
                        on_delta=self.ui.print_assistant_delta,
                    )
                else:
                    response = await self.llm.complete(
                        self.state.history, tools=self.tool_specs, model=self.model
                    )
                self.usage.record(response.usage, time.perf_counter() - start)
            except LLMError as exc:
                self.ui.print_guardrail_stop(f"LLM call failed: {exc}")
                return

            self.state.history.append(
                LLMMessage(
                    role="assistant", content=response.content, tool_calls=response.tool_calls
                )
            )

            if not response.tool_calls:
                self._trim_history()
                if response.content:
                    if not hasattr(self.llm, "complete_stream"):
                        self.ui.print_assistant(response.content)
                    elif hasattr(self.ui, "print_assistant_delta"):
                        self.ui.console.print()
                return

            for call in response.tool_calls:
                key = (call.name, call.arguments)
                recent_calls.append(key)
                if len(recent_calls) >= REPEATED_CALL_WINDOW and all(
                    c == key for c in recent_calls[-REPEATED_CALL_WINDOW:]
                ):
                    self.ui.print_guardrail_stop(
                        f"tool {call.name!r} called with identical arguments "
                        f"{REPEATED_CALL_WINDOW} times in a row"
                    )
                    return

                observation = await self._process_tool_call(call)
                self.state.history.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=orjson.dumps(observation).decode(),
                    )
                )
                self._trim_history()

        self.ui.print_guardrail_stop(f"exceeded max steps ({self.max_steps})")

    def _trim_history(self) -> None:
        if len(self.state.history) <= self.max_history_messages:
            return
        system = [message for message in self.state.history if message.role == "system"][:1]
        if self.history_summary_enabled:
            old = self.state.history[len(system) : -(self.max_history_messages - len(system) - 1)]
            if old:
                snippets = []
                for message in old[-12:]:
                    text = (message.content or "").replace("\n", " ")
                    if text:
                        snippets.append(f"{message.role}: {text[:240]}")
                if snippets:
                    system.append(
                        LLMMessage(
                            role="system",
                            content="[Earlier conversation summarized]\n" + "\n".join(snippets),
                        )
                    )
        tail = self.state.history[-(self.max_history_messages - len(system)):]
        while tail and tail[0].role in {"tool", "assistant"}:
            tail = tail[1:]
        self.state.history = system + tail

    async def _process_tool_call(self, call: ToolCall) -> dict:
        tool = self.tools_by_name.get(call.name)
        if tool is None:
            error = f"unknown tool: {call.name}"
            self.ui.print_tool_error(call.name, error)
            return {"error": error}

        self.ui.print_tool_call(call.name, call.arguments)

        if requires_approval(tool.risk_tier) and call.name not in self.state.always_allowed:
            decision = self.ui.confirm_tool_call(tool, call.arguments)
            if decision == "always":
                self.state.always_allowed.add(call.name)
            elif decision == "no":
                self.ui.print_tool_declined(call.name)
                return {"error": "user declined this tool call"}

        try:
            result = await tool.handler(call.arguments)
        except Exception as exc:  # noqa: BLE001 - tool failures become observations, not crashes
            self.ui.print_tool_error(call.name, str(exc))
            return {"error": str(exc)}

        return truncate_result(result, max_chars=self.result_max_chars)
