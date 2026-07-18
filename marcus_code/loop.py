import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import orjson

from harness.db.enums import RiskTier
from harness.llm.gateway import LLMError, LLMGateway, LLMTransientError
from harness.llm.types import LLMMessage, ToolCall, ToolSpec, Usage
from harness.runtime.guardrails import REPEATED_CALL_WINDOW
from harness.runtime.result_pipeline import truncate_result
from harness.runtime.tool_validation import ToolArgumentError, normalize_and_validate_arguments
from harness.runtime.tools import Tool
from marcus_code.modes import AgentMode, tool_is_allowed, tool_requires_approval
from marcus_code.task_contract import TaskKind, derive_task_contract, is_verification_evidence
from marcus_code.token_utils import (
    estimate_message_tokens,
    summarize_tool_result,
    trim_messages_to_budget,
)
from marcus_code.ui import TerminalUI

DEFAULT_MAX_STEPS = 100
DEFAULT_RESULT_MAX_CHARS = 4000
DEFAULT_MAX_HISTORY_MESSAGES = 100
DEFAULT_MAX_FINALIZATION_REPAIRS = 3
DEFAULT_MAX_PARALLEL_READ_ONLY = 3

_TASK_KIND_HINTS = {
    TaskKind.explain: (
        "This is an explanation request. Read the relevant files first, "
        "then summarize what you found. Do not answer before you have gathered the facts."
    ),
    TaskKind.change: (
        "This is a code-change request. Follow the sequence: explore → plan → change → verify. "
        "State a concise plan before the first mutation, and run a verification tool before finishing."
    ),
    TaskKind.operate: (
        "This is an operation request. Use start_process → wait_for_http → run client commands "
        "→ stop_process. Always stop background processes you start."
    ),
}


@dataclass
class SessionState:
    """Everything that persists across turns within one REPL session (in
    memory only for Phase 1 — see docs/marcus-code-handoff.md)."""

    history: list[LLMMessage] = field(default_factory=list)
    always_allowed: set[str] = field(default_factory=set)
    last_turn_input: str | None = None
    last_turn_guardrail: str | None = None
    # Cache for idempotent read-only tools keyed by (name, sorted-argument JSON).
    tool_result_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


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
        max_finalization_repairs: int = DEFAULT_MAX_FINALIZATION_REPAIRS,
        max_parallel_read_only: int = DEFAULT_MAX_PARALLEL_READ_ONLY,
    ) -> None:
        self.llm = llm
        self.tools_by_name = {t.name: t for t in tools}
        self.all_tool_specs = [t.to_spec() for t in tools]
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
        self.max_finalization_repairs = max_finalization_repairs
        self.max_parallel_read_only = max_parallel_read_only
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
        self.state.last_turn_input = user_input
        self.state.last_turn_guardrail = None
        kind_hint = _TASK_KIND_HINTS.get(contract.kind)
        if kind_hint:
            self.state.history.append(LLMMessage(role="system", content=kind_hint))
        plan_shown = False
        verification_succeeded = False
        finalization_repairs = 0
        retried_after_timeout = False
        outcome_fingerprints: list[tuple[str, str]] = []
        identical_call_recovery_attempts = 0
        identical_call_last_key: tuple[str, str] | None = None

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
                if hasattr(self.ui, "start_thinking"):
                    self.ui.start_thinking()
                use_stream = hasattr(self.llm, "complete_stream") and hasattr(
                    self.ui, "stream_delta"
                )
                active_specs = self._active_tool_specs(contract.kind)
                async with asyncio.timeout(self.llm_recovery_timeout_seconds):
                    if use_stream:
                        try:
                            if hasattr(self.ui, "start_stream"):
                                self.ui.start_stream()

                            def _on_delta(delta_text: str) -> None:
                                if hasattr(self.ui, "stream_delta"):
                                    self.ui.stream_delta(delta_text)

                            response = await self.llm.complete_stream(
                                self.state.history,
                                tools=active_specs,
                                model=self.model,
                                # One retry here, then one bounded standard fallback.
                                max_retries=1,
                                on_delta=_on_delta,
                            )
                            if hasattr(self.ui, "end_stream"):
                                self.ui.end_stream()
                        except LLMTransientError:
                            if hasattr(self.ui, "end_stream"):
                                self.ui.end_stream()
                            if hasattr(self.ui, "print_recovery"):
                                self.ui.print_recovery(
                                    "Streaming failed; recovering with a standard request."
                                )
                            response = await self.llm.complete(
                                self.state.history,
                                tools=active_specs,
                                model=self.model,
                                max_retries=1,
                            )
                    else:
                        response = await self.llm.complete(
                            self.state.history,
                            tools=active_specs,
                            model=self.model,
                            max_retries=1,
                        )
                duration = time.perf_counter() - start
                if hasattr(self.ui, "stop_thinking"):
                    self.ui.stop_thinking(duration)
                self.usage.record(response.usage, duration)
                self._update_ui_status()
            except LLMError as exc:
                if hasattr(self.ui, "end_stream"):
                    self.ui.end_stream()
                if hasattr(self.ui, "stop_thinking"):
                    self.ui.stop_thinking(0.0)
                self.ui.print_guardrail_stop(f"LLM call failed: {exc}")
                return
            except TimeoutError:
                if hasattr(self.ui, "end_stream"):
                    self.ui.end_stream()
                if hasattr(self.ui, "stop_thinking"):
                    self.ui.stop_thinking(0.0)
                if not retried_after_timeout:
                    retried_after_timeout = True
                    self.compact_history()
                    if hasattr(self.ui, "print_recovery"):
                        self.ui.print_recovery(
                            "LLM timed out; retrying once with compacted context."
                        )
                    continue
                self.state.last_turn_guardrail = (
                    "LLM recovery timed out after "
                    f"{self.llm_recovery_timeout_seconds:g}s; returned control safely"
                )
                self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                return

            self.state.history.append(
                LLMMessage(
                    role="assistant", content=response.content, tool_calls=response.tool_calls
                )
            )

            if not response.tool_calls:
                if contract.requires_verification and not verification_succeeded:
                    if finalization_repairs < self.max_finalization_repairs:
                        finalization_repairs += 1
                        hint = contract.missing_evidence_hint(verification_succeeded)
                        self.state.history.append(
                            LLMMessage(
                                role="system",
                                content=(
                                    f"Finalization denied by runtime (repair {finalization_repairs}/"
                                    f"{self.max_finalization_repairs}): {hint}. "
                                    "Run one appropriate verification tool, then summarize its result."
                                ),
                            )
                        )
                        continue
                    self.state.last_turn_guardrail = (
                        "final answer blocked: requested verification has no successful evidence"
                    )
                    self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
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
                self.ui.print_assistant(
                    "Plan: inspect the current state, make the change, then verify it."
                )
                plan_shown = True

            selected_calls, rejected_calls = self._plan_tool_calls(response.tool_calls)

            # Reject calls that exceed the per-step policy before execution.
            for call in rejected_calls:
                policy_reason = self._policy_reason_for_rejected(call, selected_calls)
                self.ui.print_tool_error(call.name, policy_reason)
                observation = {
                    "status": "error",
                    "error": policy_reason,
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

            # Execute selected calls: read-only calls may run concurrently,
            # mutation calls are processed sequentially (at most one here).
            observations = await self._execute_selected_calls(selected_calls)

            # Process results in response order so guardrails remain sequential.
            force_rethink = False
            for call, observation in observations:
                key = (call.name, call.arguments)
                recent_calls.append(key)
                if len(recent_calls) >= REPEATED_CALL_WINDOW and all(
                    c == key for c in recent_calls[-REPEATED_CALL_WINDOW:]
                ):
                    if identical_call_recovery_attempts < 2:
                        identical_call_recovery_attempts += 1
                        identical_call_last_key = key
                        recovery_message = (
                            f"Guardrail: tool {call.name!r} has been called with "
                            f"the same arguments {REPEATED_CALL_WINDOW} times in a row. "
                            f"You are now in recovery attempt "
                            f"{identical_call_recovery_attempts}/2. "
                            "Stop repeating that call; analyze the situation and choose a "
                            "different tool or different arguments before proceeding."
                        )
                        if hasattr(self.ui, "print_recovery"):
                            self.ui.print_recovery(recovery_message)
                        self.state.history.append(
                            LLMMessage(role="system", content=recovery_message)
                        )
                        force_rethink = True
                        break  # stop processing further observations and force a rethink
                    self.state.last_turn_guardrail = (
                        f"tool {call.name!r} called with identical arguments "
                        f"{REPEATED_CALL_WINDOW} times in a row; "
                        f"recovery budget exhausted ({identical_call_recovery_attempts}/2)"
                    )
                    self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                    return

                if key == identical_call_last_key:
                    # The rest of this step's observations are skipped because we
                    # are forcing a rethink after a repeated identical call.
                    continue

                if is_verification_evidence(call.name, call.arguments, observation):
                    verification_succeeded = True
                fingerprint = (
                    call.name,
                    str(
                        observation.get("code")
                        or orjson.dumps(observation, option=orjson.OPT_SORT_KEYS).decode()
                    ),
                )
                outcome_fingerprints.append(fingerprint)
                if len(outcome_fingerprints) >= 3 and all(
                    item == fingerprint for item in outcome_fingerprints[-3:]
                ):
                    # During identical-call recovery the LLM is being given a chance
                    # to change strategy, so a single non-identical outcome should
                    # reset the no-progress counter rather than stop immediately.
                    if (
                        identical_call_last_key is not None
                        and key != identical_call_last_key
                    ):
                        outcome_fingerprints.clear()
                        outcome_fingerprints.append(fingerprint)
                    else:
                        self.state.last_turn_guardrail = (
                            f"no progress after 3 calls to {call.name!r}; stopped to prevent a loop"
                        )
                        self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
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
                        self.state.last_turn_guardrail = (
                            f"argument repair budget exhausted for {call.name!r} "
                            f"({self.max_argument_repairs})"
                        )
                        self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                        return
                if "error" in observation and "declined" not in str(observation["error"]):
                    consecutive_tool_failures += 1
                else:
                    consecutive_tool_failures = 0

                if force_rethink:
                    # The identical-call guardrail forced a rethink; do not append
                    # this observation as a normal tool result because the LLM should
                    # see only the recovery system message and choose a new strategy.
                    break

                # Compress verbose tool results before storing to save tokens.
                compressed = summarize_tool_result(orjson.dumps(observation).decode())
                self.state.history.append(
                    LLMMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=compressed,
                    )
                )
                self._trim_history()
                if consecutive_tool_failures >= self.max_consecutive_tool_failures:
                    self.state.last_turn_guardrail = (
                        "too many consecutive tool failures "
                        f"({self.max_consecutive_tool_failures}); stopped to prevent a retry loop"
                    )
                    self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                    return

        self.state.last_turn_guardrail = f"exceeded max steps ({self.max_steps})"
        self.ui.print_guardrail_stop(self.state.last_turn_guardrail)

    _TOOL_SETS_BY_TASK_KIND: dict[str, set[str]] = {
        "explain": {
            "read_file",
            "list_files",
            "grep",
            "fetch_url",
            "search_web",
            "git_status",
            "git_diff",
            "git_log",
            "read_directory_tree",
            "summarize_text",
            "compare_files",
            "ask_user_choice",
            "load_skill",
        },
        "change": {
            "read_file",
            "write_file",
            "edit_file",
            "apply_diff",
            "list_files",
            "grep",
            "run_tests",
            "execute_python",
            "run_cli",
            "git_status",
            "git_diff",
            "read_directory_tree",
            "summarize_text",
            "compare_files",
            "ask_user_choice",
            "load_skill",
            "memory_read",
            "memory_write",
            "todo_create",
            "todo_update",
            "todo_list",
        },
        "operate": {
            "run_cli",
            "start_process",
            "read_process_output",
            "stop_process",
            "wait_for_http",
            "list_processes",
            "kill_process",
            "check_url_health",
            "fetch_url",
            "read_file",
            "list_files",
            "ask_user_choice",
            "load_skill",
            "memory_read",
            "memory_write",
            "todo_create",
            "todo_update",
            "todo_list",
        },
    }

    def _active_tool_specs(self, task_kind: TaskKind) -> list[ToolSpec]:
        """Return the subset of tools disclosed to the model for this task kind."""
        allowed = self._TOOL_SETS_BY_TASK_KIND.get(task_kind.value)
        if allowed is None:
            return list(self.all_tool_specs)
        return [spec for spec in self.all_tool_specs if spec.name in allowed]

    def _trim_history(self) -> None:
        if not self.state.history:
            return

        # First: summarize old prior turns before count-cap trimming.
        if self.history_summary_enabled and len(self.state.history) > self.max_history_messages:
            first_system = [self.state.history[0]] if self.state.history[0].role == "system" else []
            latest_user_index = next(
                (
                    i
                    for i in range(len(self.state.history) - 1, -1, -1)
                    if self.state.history[i].role == "user"
                ),
                None,
            )
            if latest_user_index is not None and latest_user_index > len(first_system) + 1:
                prior_turns = self.state.history[len(first_system) : latest_user_index]
                current_turn = self.state.history[latest_user_index:]
                summary = self._summarize_prior_turns(prior_turns)
                if summary:
                    self.state.history = first_system + [summary] + current_turn

        # Second: message-count cap, preserving system + latest user + any summary.
        if len(self.state.history) > self.max_history_messages:
            first_system = [self.state.history[0]] if self.state.history[0].role == "system" else []
            latest_user_index = next(
                (
                    i
                    for i in range(len(self.state.history) - 1, -1, -1)
                    if self.state.history[i].role == "user"
                ),
                None,
            )
            # Build a candidate list with system, a summary right after it, and the latest user onward.
            candidate: list[LLMMessage] = list(first_system)
            if len(self.state.history) > len(first_system) and self.state.history[
                len(first_system)
            ].role in {"system", "assistant"}:
                candidate.append(self.state.history[len(first_system)])
            if latest_user_index is not None:
                candidate.extend(self.state.history[latest_user_index:])
            # Drop the oldest non-protected assistant/tool messages first to fit the cap,
            # preserving the system prompt, any summary, and the latest user.
            while len(candidate) > self.max_history_messages:
                drop_idx: int | None = None
                for i in range(len(first_system), len(candidate)):
                    if i == len(first_system) and candidate[i].role == "system":
                        continue  # keep summary
                    if candidate[i].role in {"tool", "assistant"}:
                        drop_idx = i
                        break
                if drop_idx is None:
                    break
                candidate.pop(drop_idx)
            # Last resort: drop the summary if we still cannot fit.
            while len(candidate) > self.max_history_messages and len(candidate) > len(first_system):
                candidate.pop(len(first_system))
            self.state.history = candidate

        # Third: token-budget trim by importance, preserving system + latest user.
        token_budget = min(
            self.context_window_tokens,
            int(self.context_window_tokens * self.compact_threshold_percent / 100),
        )
        self.state.history = trim_messages_to_budget(
            self.state.history,
            token_budget,
            preserve_system=True,
            preserve_latest_user=True,
        )

    def _summarize_prior_turns(self, messages: list[LLMMessage]) -> LLMMessage | None:
        if not messages or not self.history_summary_enabled:
            return None
        facts = self._extract_facts(messages)
        prior_outcomes: list[str] = []
        for message in reversed(messages):
            if message.role == "assistant" and message.content:
                prior_outcomes.insert(0, message.content[:240])
            if len(prior_outcomes) >= 6:
                break
        summary_parts = []
        if prior_outcomes:
            summary_parts.append(
                "[Prior turns summarized]\n" + "\n".join(f"- {text}" for text in prior_outcomes)
            )
        if facts:
            summary_parts.append("[Preserved facts from earlier steps]\n" + "\n".join(facts))
        if not summary_parts:
            return None
        return LLMMessage(role="system", content="\n\n".join(summary_parts))

    @staticmethod
    def _extract_facts(messages: list[LLMMessage]) -> list[str]:
        """Pull concrete facts out of old tool result messages before they are summarized away."""
        facts: list[str] = []
        for message in messages:
            if message.role != "tool" or not message.content:
                continue
            try:
                data = orjson.loads(message.content)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if "exit_code" in data:
                facts.append(f"{message.name}: exit_code={data['exit_code']}")
            if "status" in data:
                facts.append(f"{message.name}: status={data['status']}")
            if "ready" in data and data.get("ready"):
                facts.append(f"{message.name}: ready={data['ready']}")
            if "process_id" in data:
                facts.append(f"{message.name}: process_id={data['process_id']}")
            if "files" in data and isinstance(data["files"], list):
                facts.append(f"{message.name}: listed {len(data['files'])} file(s)")
            if "matches" in data and isinstance(data["matches"], list):
                facts.append(f"{message.name}: found {len(data['matches'])} match(es)")
        return facts

    @property
    def context_tokens(self) -> int:
        """More accurate per-message token estimate for retained history."""
        return max(1, sum(estimate_message_tokens(m) for m in self.state.history))

    @property
    def context_percent(self) -> float:
        return min(100.0, self.context_tokens * 100 / max(1, self.context_window_tokens))

    def compact_history(self) -> tuple[int, int]:
        before = self.context_tokens
        target = self.context_window_tokens * self.compact_target_percent // 100

        # First pass: summarize old prior turns into a compact system message.
        if self.history_summary_enabled and len(self.state.history) > 4:
            first_system = [self.state.history[0]] if self.state.history[0].role == "system" else []
            latest_user_index = next(
                (
                    i
                    for i in range(len(self.state.history) - 1, -1, -1)
                    if self.state.history[i].role == "user"
                ),
                None,
            )
            if latest_user_index is not None and latest_user_index > len(first_system) + 1:
                prior_turns = self.state.history[len(first_system) : latest_user_index]
                current_turn = self.state.history[latest_user_index:]
                summary = self._summarize_prior_turns(prior_turns)
                if summary:
                    self.state.history = first_system + [summary] + current_turn

        # Second pass: importance-based token trimming until we hit target.
        while self.context_tokens > target and len(self.state.history) > 4:
            previous = self.context_tokens
            self.state.history = trim_messages_to_budget(
                self.state.history,
                target,
                preserve_system=True,
                preserve_latest_user=True,
            )
            if self.context_tokens >= previous:
                break

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

    def _plan_tool_calls(self, tool_calls: list[ToolCall]) -> tuple[list[ToolCall], list[ToolCall]]:
        """Select which tool calls from one LLM response may execute.

        Mutation calls run one at a time. Read-only calls may run concurrently
        up to ``max_parallel_read_only`` when they appear first in the response.
        """
        if not tool_calls:
            return [], []

        first_tier = self.tools_by_name.get(tool_calls[0].name, Tool).risk_tier
        if first_tier != RiskTier.read_only:
            # Mutation-first response: run only the first call.
            return [tool_calls[0]], list(tool_calls[1:])

        selected: list[ToolCall] = []
        rejected: list[ToolCall] = []
        for call in tool_calls:
            tool = self.tools_by_name.get(call.name)
            if tool is None:
                rejected.append(call)
                continue
            if tool.risk_tier != RiskTier.read_only:
                rejected.append(call)
                continue
            if len(selected) < self.max_parallel_read_only:
                selected.append(call)
            else:
                rejected.append(call)
        return selected, rejected

    def _policy_reason_for_rejected(self, call: ToolCall, selected_calls: list[ToolCall]) -> str:
        tool = self.tools_by_name.get(call.name)
        if tool is None:
            return f"unknown tool: {call.name}"
        if tool.risk_tier != RiskTier.read_only:
            return (
                "mutation tool calls run one at a time; "
                "inspect the result before the next write/execute action"
            )
        if len(selected_calls) >= self.max_parallel_read_only:
            return (
                f"only {self.max_parallel_read_only} read-only tool calls "
                "are allowed per reasoning step"
            )
        return "tool call rejected by per-step policy"

    async def _execute_selected_calls(
        self, selected_calls: list[ToolCall]
    ) -> list[tuple[ToolCall, dict]]:
        """Run selected calls and return (call, observation) tuples in order."""
        if not selected_calls:
            return []

        # Separate mutation calls (sequential) from read-only calls (parallel).
        read_only_calls: list[ToolCall] = []
        mutation_calls: list[ToolCall] = []
        for call in selected_calls:
            tool = self.tools_by_name.get(call.name)
            if tool is not None and tool.risk_tier == RiskTier.read_only:
                read_only_calls.append(call)
            else:
                mutation_calls.append(call)

        results: dict[str, dict] = {}

        # Validate arguments first so we fail fast before any side effects.
        for call in read_only_calls:
            tool = self.tools_by_name.get(call.name)
            if tool is None:
                continue
            try:
                call.arguments = normalize_and_validate_arguments(tool.parameters, call.arguments)
            except ToolArgumentError as exc:
                results[call.id] = {"error": str(exc), "code": exc.code, "retryable": True}

        async def _run_read_only(call: ToolCall) -> tuple[str, dict]:
            if call.id in results:
                return call.id, results[call.id]
            tool = self.tools_by_name.get(call.name)
            cache_key: tuple[str, str] | None = None
            if tool is not None and tool.idempotent:
                cache_key = (call.name, self._cache_key_for_arguments(call.arguments))
                cached = self.state.tool_result_cache.get(cache_key)
                if cached is not None:
                    self.ui.print_tool_call(call.name, call.arguments)
                    if hasattr(self.ui, "print_tool_result"):
                        self.ui.print_tool_result(call.name, cached)
                    return call.id, cached
            observation = await self._invoke_tool_handler(call)
            if cache_key is not None:
                self.state.tool_result_cache[cache_key] = observation
            return call.id, observation

        read_only_results = await asyncio.gather(
            *(_run_read_only(call) for call in read_only_calls), return_exceptions=True
        )
        for item in read_only_results:
            if isinstance(item, BaseException):
                # Should not happen because _invoke_tool_handler catches exceptions,
                # but guard against unexpected errors from parallel scheduling.
                continue
            call_id, observation = item
            results[call_id] = observation

        # Mutation calls are processed sequentially after read-only calls.
        for call in mutation_calls:
            results[call.id] = await self._process_tool_call(call)

        return [(call, results[call.id]) for call in selected_calls]

    @staticmethod
    def _cache_key_for_arguments(arguments: dict[str, Any]) -> str:
        return orjson.dumps(arguments, option=orjson.OPT_SORT_KEYS).decode()

    async def _invoke_tool_handler(self, call: ToolCall) -> dict:
        """Execute a single tool call without approval or policy checks.

        Used for read-only calls that have already been validated and selected.
        """
        tool = self.tools_by_name.get(call.name)
        if tool is None:
            error = f"unknown tool: {call.name}"
            self.ui.print_tool_error(call.name, error)
            return {"error": error}

        self.ui.print_tool_call(call.name, call.arguments)

        if not tool_is_allowed(self.mode, tool.risk_tier):
            error = f"tool {call.name!r} is not available in {self.mode.value} mode"
            self.ui.print_tool_error(call.name, error)
            return {"error": error}

        retryable = (tool.risk_tier.value == "read_only" or tool.idempotent) and call.name not in {
            "wait_for_http"
        }
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

    async def _process_tool_call(self, call: ToolCall) -> dict:
        """Full single-tool execution path including validation and approval."""
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

        if (
            tool_requires_approval(
                self.mode,
                tool_name=call.name,
                risk_tier=tool.risk_tier,
                arguments=call.arguments,
            )
            and call.name not in self.state.always_allowed
        ):
            decision = self.ui.confirm_tool_call(tool, call.arguments)
            if decision == "always":
                self.state.always_allowed.add(call.name)
            elif decision == "no":
                self.ui.print_tool_declined(call.name)
                return {"error": "user declined this tool call"}

        return await self._invoke_tool_handler(call)
