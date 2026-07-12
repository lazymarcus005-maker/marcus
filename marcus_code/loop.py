import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

import orjson

from harness.llm.gateway import LLMError, LLMGateway, LLMTransientError
from harness.llm.types import LLMMessage, ToolCall, Usage
from harness.runtime.guardrails import REPEATED_CALL_WINDOW
from harness.runtime.result_pipeline import truncate_result
from harness.runtime.tool_validation import ToolArgumentError, normalize_and_validate_arguments
from harness.runtime.tools import Tool
from marcus_code.modes import AgentMode, tool_is_allowed, tool_requires_approval
from marcus_code.task_contract import derive_task_contract, is_verification_evidence
from marcus_code.ui import TerminalUI

DEFAULT_MAX_STEPS = 100
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
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    compactions: int = 0
    last_elapsed_seconds: float = 0.0

    def record(self, usage: Usage, duration: float) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.llm_calls += 1
        self.elapsed_seconds += duration
        self.last_prompt_tokens = usage.prompt_tokens
        self.last_completion_tokens = usage.completion_tokens
        self.last_elapsed_seconds = duration

    @property
    def tokens_per_second(self) -> float:
        return self.total_tokens / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    @property
    def last_tokens_per_second(self) -> float:
        if self.last_elapsed_seconds <= 0:
            return 0.0
        return self.last_completion_tokens / self.last_elapsed_seconds


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
        mode: AgentMode = AgentMode.agent,
        context_window_tokens: int = 131_072,
        compact_threshold_percent: int = 85,
        compact_target_percent: int = 60,
        max_consecutive_tool_failures: int = 5,
        max_safe_tool_retries: int = 2,
        llm_recovery_timeout_seconds: float = 90.0,
        max_tool_calls_per_step: int = 1,
        max_argument_repairs: int = 1,
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
        self.mode = mode
        self.context_window_tokens = context_window_tokens
        self.compact_threshold_percent = compact_threshold_percent
        self.compact_target_percent = compact_target_percent
        self.max_consecutive_tool_failures = max_consecutive_tool_failures
        self.max_safe_tool_retries = max_safe_tool_retries
        self.llm_recovery_timeout_seconds = llm_recovery_timeout_seconds
        self.max_tool_calls_per_step = max_tool_calls_per_step
        self.max_argument_repairs = max_argument_repairs
        self.state = SessionState()
        self.usage = UsageStats()
        self.started_at = datetime.now()
        if system_prompt:
            self.state.history.append(LLMMessage(role="system", content=system_prompt))

    async def run_turn(self, user_input: str) -> None:
        if hasattr(self.ui, "begin_turn"):
            self.ui.begin_turn()
        self.state.history.append(LLMMessage(role="user", content=user_input))
        recent_calls: list[tuple[str, dict]] = []
        consecutive_tool_failures = 0
        argument_failures: dict[str, int] = {}
        contract = derive_task_contract(user_input)
        plan_shown = False
        verification_succeeded = False
        finalization_repairs = 0
        outcome_fingerprints: list[tuple[str, str]] = []

        for _ in range(self.max_steps):
            self._trim_history()
            if self.context_percent >= self.compact_threshold_percent:
                self.compact_history()
            if self.context_tokens >= self.context_window_tokens:
                self.ui.print_guardrail_stop(
                    "retained context exceeds the configured context window; use /compact or /clear"
                )
                return
            if (
                self.max_total_tokens is not None
                and self.usage.total_tokens >= self.max_total_tokens
            ):
                self.ui.print_guardrail_stop(
                    f"session token budget exceeded ({self.max_total_tokens})"
                )
                return
            try:
                start = time.perf_counter()
                use_stream = hasattr(self.llm, "complete_stream") and hasattr(
                    self.ui, "print_assistant_delta"
                )
                async with asyncio.timeout(self.llm_recovery_timeout_seconds):
                    if use_stream:
                        try:
                            response = await self.llm.complete_stream(
                                self.state.history,
                                tools=self.tool_specs,
                                model=self.model,
                                # One retry here, then one bounded standard fallback.
                                max_retries=1,
                                on_delta=None,
                            )
                        except LLMTransientError:
                            if hasattr(self.ui, "print_recovery"):
                                self.ui.print_recovery(
                                    "Streaming failed; recovering with a standard request."
                                )
                            response = await self.llm.complete(
                                self.state.history,
                                tools=self.tool_specs,
                                model=self.model,
                                max_retries=1,
                            )
                    else:
                        response = await self.llm.complete(
                            self.state.history,
                            tools=self.tool_specs,
                            model=self.model,
                            max_retries=1,
                        )
                self.usage.record(response.usage, time.perf_counter() - start)
                self._update_ui_status()
            except LLMError as exc:
                self.ui.print_guardrail_stop(f"LLM call failed: {exc}")
                return
            except TimeoutError:
                self.ui.print_guardrail_stop(
                    "LLM recovery timed out after "
                    f"{self.llm_recovery_timeout_seconds:g}s; returned control safely"
                )
                return

            self.state.history.append(
                LLMMessage(
                    role="assistant", content=response.content, tool_calls=response.tool_calls
                )
            )

            if not response.tool_calls:
                if contract.requires_verification and not verification_succeeded:
                    if finalization_repairs < 1:
                        finalization_repairs += 1
                        self.state.history.append(
                            LLMMessage(
                                role="system",
                                content=(
                                    "Finalization denied by runtime: this task explicitly requires "
                                    "verification, but no successful build/test/HTTP evidence exists. "
                                    "Run one appropriate verification tool, then summarize its result."
                                ),
                            )
                        )
                        continue
                    self.ui.print_guardrail_stop(
                        "final answer blocked: requested verification has no successful evidence"
                    )
                    return
                self._trim_history()
                if response.content:
                    if hasattr(self.ui, "print_final_answer"):
                        self.ui.print_final_answer(response.content)
                    else:
                        self.ui.print_assistant(response.content)
                elif hasattr(self.ui, "finish_steps"):
                    self.ui.finish_steps(success=True)
                return

            if response.content:
                self.ui.print_assistant(response.content)
                plan_shown = True
            elif contract.requires_plan and not plan_shown:
                self.ui.print_assistant("Plan: inspect the current state, make the change, then verify it.")
                plan_shown = True

            for call_index, call in enumerate(response.tool_calls):
                if call_index >= self.max_tool_calls_per_step:
                    error = (
                        "only one tool call is allowed per reasoning step; "
                        "inspect its result before choosing the next action"
                    )
                    self.ui.print_tool_error(call.name, error)
                    observation = {
                        "status": "error",
                        "error": error,
                        "code": "POLICY_DENIED",
                        "retryable": True,
                    }
                    self.state.history.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content=orjson.dumps(observation).decode(),
                        )
                    )
                    continue
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
                if is_verification_evidence(call.name, call.arguments, observation):
                    verification_succeeded = True
                fingerprint = (
                    call.name,
                    str(observation.get("code") or orjson.dumps(observation, option=orjson.OPT_SORT_KEYS).decode()),
                )
                outcome_fingerprints.append(fingerprint)
                if len(outcome_fingerprints) >= 3 and all(
                    item == fingerprint for item in outcome_fingerprints[-3:]
                ):
                    self.ui.print_guardrail_stop(
                        f"no progress after 3 calls to {call.name!r}; stopped to prevent a loop"
                    )
                    return
                if observation.get("code") == "INVALID_ARGUMENT":
                    argument_failures[call.name] = argument_failures.get(call.name, 0) + 1
                    if argument_failures[call.name] > self.max_argument_repairs:
                        self.state.history.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=call.id,
                                name=call.name,
                                content=orjson.dumps(observation).decode(),
                            )
                        )
                        self.ui.print_guardrail_stop(
                            f"argument repair budget exhausted for {call.name!r} "
                            f"({self.max_argument_repairs})"
                        )
                        return
                if "error" in observation and "declined" not in str(observation["error"]):
                    consecutive_tool_failures += 1
                else:
                    consecutive_tool_failures = 0
                self.state.history.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=orjson.dumps(observation).decode(),
                    )
                )
                self._trim_history()
                if consecutive_tool_failures >= self.max_consecutive_tool_failures:
                    self.ui.print_guardrail_stop(
                        "too many consecutive tool failures "
                        f"({self.max_consecutive_tool_failures}); stopped to prevent a retry loop"
                    )
                    return

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
        tail = self.state.history[-(self.max_history_messages - len(system)) :]
        while tail and tail[0].role in {"tool", "assistant"}:
            tail = tail[1:]
        self.state.history = system + tail

    @property
    def context_tokens(self) -> int:
        """Conservative estimate of retained messages sent on the next request."""
        serialized = orjson.dumps([message.to_openai() for message in self.state.history])
        return max(1, (len(serialized) + 3) // 4)

    @property
    def context_percent(self) -> float:
        return min(100.0, self.context_tokens * 100 / max(1, self.context_window_tokens))

    def compact_history(self) -> tuple[int, int]:
        before = self.context_tokens
        target = self.context_window_tokens * self.compact_target_percent // 100
        original_limit = self.max_history_messages
        while self.context_tokens > target and len(self.state.history) > 4:
            self.max_history_messages = max(4, min(self.max_history_messages - 1, len(self.state.history) - 1))
            self._trim_history()
        self.max_history_messages = original_limit
        after = self.context_tokens
        if after < before:
            self.usage.compactions += 1
        self._update_ui_status()
        return before, after

    def clear_history(self, *, clear_all: bool = False) -> None:
        system = next((message for message in self.state.history if message.role == "system"), None)
        self.state.history = [system] if system is not None else []
        if clear_all:
            self.state.always_allowed.clear()
        self._update_ui_status()

    def status(self, workspace: str) -> dict:
        return {
            "session_started_at": self.started_at,
            "model": self.model or "default",
            "mode": self.mode.value,
            "workspace": workspace,
            "context_tokens": self.context_tokens,
            "context_limit": self.context_window_tokens,
            "total_tokens": self.usage.total_tokens,
            "tokens_per_second": self.usage.last_tokens_per_second,
        }

    def _update_ui_status(self) -> None:
        if hasattr(self.ui, "refresh_status"):
            self.ui.refresh_status()

    async def _process_tool_call(self, call: ToolCall) -> dict:
        tool = self.tools_by_name.get(call.name)
        if tool is None:
            error = f"unknown tool: {call.name}"
            self.ui.print_tool_error(call.name, error)
            return {"error": error}

        try:
            call.arguments = normalize_and_validate_arguments(tool.parameters, call.arguments)
        except ToolArgumentError as exc:
            self.ui.print_tool_error(call.name, str(exc))
            return {"error": str(exc), "code": exc.code, "retryable": True}

        self.ui.print_tool_call(call.name, call.arguments)

        if not tool_is_allowed(self.mode, tool.risk_tier):
            error = f"tool {call.name!r} is not available in {self.mode.value} mode"
            self.ui.print_tool_error(call.name, error)
            return {"error": error}

        if tool_requires_approval(
            self.mode,
            tool_name=call.name,
            risk_tier=tool.risk_tier,
            arguments=call.arguments,
        ) and call.name not in self.state.always_allowed:
            decision = self.ui.confirm_tool_call(tool, call.arguments)
            if decision == "always":
                self.state.always_allowed.add(call.name)
            elif decision == "no":
                self.ui.print_tool_declined(call.name)
                return {"error": "user declined this tool call"}

        retryable = (
            (tool.risk_tier.value == "read_only" or tool.idempotent)
            and call.name not in {"wait_for_http"}
        )
        max_attempts = self.max_safe_tool_retries + 1 if retryable else 1
        for attempt in range(max_attempts):
            try:
                result = await tool.handler(call.arguments)
                break
            except Exception as exc:  # noqa: BLE001 - failures become observations
                if attempt + 1 < max_attempts:
                    if hasattr(self.ui, "print_recovery"):
                        self.ui.print_recovery(
                            f"Tool {call.name} failed; retrying safely "
                            f"({attempt + 2}/{max_attempts})."
                        )
                    continue
                self.ui.print_tool_error(call.name, str(exc))
                return {"error": str(exc)}

        observation = truncate_result(result, max_chars=self.result_max_chars)
        if hasattr(self.ui, "print_tool_result"):
            self.ui.print_tool_result(call.name, observation)
        return observation
