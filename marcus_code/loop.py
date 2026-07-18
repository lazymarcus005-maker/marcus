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
from marcus_code.task_contract import (
    Capability,
    ResponseMode,
    TaskContract,
    TaskKind,
    VerificationPolicy,
    derive_task_contract,
    is_verification_attempt,
    is_verification_evidence,
)
from marcus_code.todo_tracker import Phase, TodoTracker
from marcus_code.token_utils import (
    estimate_message_tokens,
    summarize_tool_result,
    trim_messages_to_budget,
    trim_messages_to_count,
)
from marcus_code.ui import TerminalUI

DEFAULT_MAX_STEPS = 100
DEFAULT_RESULT_MAX_CHARS = 4000
DEFAULT_MAX_HISTORY_MESSAGES = 100
DEFAULT_MAX_FINALIZATION_REPAIRS = 3
DEFAULT_MAX_PARALLEL_READ_ONLY = 3

_TASK_KIND_HINTS = {
    TaskKind.explain: (
        "This request needs workspace evidence. Inspect only the relevant files, "
        "then answer from the gathered facts."
    ),
    TaskKind.change: (
        "This is a code-change request. Follow the active plan: explore → change → verify. "
        "Run verification after the most recent mutation before finishing."
    ),
    TaskKind.operate: (
        "This is an operation request. Use start_process → wait_for_http → run client commands "
        "→ stop_process. Always stop background processes you start."
    ),
}

_DIRECT_HINT = (
    "This is a direct-answer request. No external tools are needed or available for this turn. "
    "Answer the user's latest question immediately and concisely from existing knowledge."
)

_PLAN_HINT = (
    "Planning phase: return a concise, task-specific plan only. Include the goal, the evidence "
    "you need to inspect, the actions you may take, how you will verify the result, and the "
    "finish condition. Do not claim that any action has run yet."
)


@dataclass
class SessionState:
    """Everything that persists across turns within one REPL session (in
    memory only for Phase 1 — see docs/marcus-code-handoff.md)."""

    history: list[LLMMessage] = field(default_factory=list)
    always_allowed: set[str] = field(default_factory=set)
    last_turn_input: str | None = None
    last_turn_guardrail: str | None = None
    active_contract: TaskContract | None = None
    active_plan: str | None = None
    active_phase: Phase = Phase.receive
    workspace_revision: int = 0
    unverified_revision: int | None = None
    verification_evidence: "VerificationEvidence | None" = None
    active_process_ids: set[str] = field(default_factory=set)
    # Cache for idempotent read-only tools keyed by (name, sorted-argument JSON).
    tool_result_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationEvidence:
    tool_name: str
    workspace_revision: int
    summary: str


def _verification_summary(tool_name: str, observation: dict[str, Any]) -> str:
    if tool_name == "run_tests":
        output = str(observation.get("stdout") or observation.get("stderr") or "").strip()
        last_line = output.splitlines()[-1] if output else "exit code 0"
        return f"run_tests: {last_line[:240]}"
    if tool_name in {"wait_for_http", "check_url_health"}:
        status = observation.get("status")
        return f"{tool_name}: HTTP {status}" if status is not None else f"{tool_name}: ready"
    if tool_name == "run_cli":
        output = str(observation.get("stdout") or observation.get("stderr") or "").strip()
        last_line = output.splitlines()[-1] if output else "exit code 0"
        return f"run_cli: {last_line[:240]}"
    return f"{tool_name}: succeeded"


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
        max_tool_calls_per_step: int = DEFAULT_MAX_PARALLEL_READ_ONLY,
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
        self.max_parallel_read_only = min(max_parallel_read_only, max_tool_calls_per_step)
        self.state = SessionState()
        self.usage = UsageStats()
        self.started_at = datetime.now()
        if system_prompt:
            self.state.history.append(LLMMessage(role="system", content=system_prompt))

    async def run_turn(self, user_input: str, *, contract: TaskContract | None = None) -> None:
        if hasattr(self.ui, "begin_turn"):
            self.ui.begin_turn()
        continuing = contract is not None
        resolved_contract = contract or derive_task_contract(user_input)
        tracker = TodoTracker()
        if hasattr(self.ui, "update_todo"):
            self.ui.update_todo(tracker)
        tracker.advance(Phase.analyze, "จำแนกคำขอและขอบเขตเครื่องมือ")
        self.state.active_phase = Phase.analyze
        if hasattr(self.ui, "update_todo"):
            self.ui.update_todo(tracker)

        # Files and external processes can change between user turns. A cache
        # is useful only inside one coherent turn and is invalidated by writes.
        self.state.tool_result_cache.clear()
        self.state.history.append(LLMMessage(role="user", content=user_input))
        recent_calls: list[tuple[str, dict]] = []
        consecutive_tool_failures = 0
        argument_failures: dict[str, int] = {}
        contract = resolved_contract
        if not continuing:
            self.state.active_plan = None
            self.state.verification_evidence = None
            self.state.last_turn_input = user_input
        self.state.active_contract = contract
        self.state.last_turn_guardrail = None
        plan_shown = bool(continuing and self.state.active_plan)
        if contract.requires_plan and not plan_shown:
            tracker.advance(Phase.plan, "สร้างแผนก่อนเรียกเครื่องมือ")
            if hasattr(self.ui, "update_todo"):
                self.ui.update_todo(tracker)
        finalization_repairs = 0
        finalization_hint: str | None = None
        retried_after_timeout = False
        outcome_fingerprints: list[tuple[str, str]] = []
        identical_call_recovery_attempts = 0
        identical_call_last_key: tuple[str, dict] | None = None

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
                planning_phase = contract.requires_plan and not plan_shown
                active_specs = [] if planning_phase else self._active_tool_specs(contract)
                if not planning_phase and self._verification_is_impossible(contract, active_specs):
                    self.state.last_turn_guardrail = "task requires verification, but no compatible verification tool is available"
                    if hasattr(self.ui, "stop_thinking"):
                        self.ui.stop_thinking(0.0)
                    self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                    return
                call_messages = list(self.state.history)
                if contract.response_mode is ResponseMode.direct:
                    call_messages.append(LLMMessage(role="system", content=_DIRECT_HINT))
                else:
                    kind_hint = _TASK_KIND_HINTS.get(contract.kind)
                    if kind_hint:
                        call_messages.append(LLMMessage(role="system", content=kind_hint))
                if planning_phase:
                    call_messages.append(LLMMessage(role="system", content=_PLAN_HINT))
                elif self.state.active_plan:
                    call_messages.append(
                        LLMMessage(
                            role="system",
                            content=f"Active plan for this turn:\n{self.state.active_plan}",
                        )
                    )
                if finalization_hint:
                    call_messages.append(LLMMessage(role="system", content=finalization_hint))
                evidence = self.state.verification_evidence
                if (
                    evidence is not None
                    and evidence.workspace_revision == self.state.workspace_revision
                ):
                    call_messages.append(
                        LLMMessage(
                            role="system",
                            content=(
                                "Current verification evidence that must be reported accurately: "
                                f"{evidence.summary}"
                            ),
                        )
                    )
                async with asyncio.timeout(self.llm_recovery_timeout_seconds):
                    if use_stream:
                        try:
                            if hasattr(self.ui, "start_stream"):
                                self.ui.start_stream()

                            def _on_delta(delta_text: str) -> None:
                                if hasattr(self.ui, "stream_delta"):
                                    self.ui.stream_delta(delta_text)

                            response = await self.llm.complete_stream(
                                call_messages,
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
                                call_messages,
                                tools=active_specs,
                                model=self.model,
                                max_retries=1,
                            )
                    else:
                        response = await self.llm.complete(
                            call_messages,
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

            if planning_phase:
                plan_text = (
                    response.content
                    if response.content is not None and self._is_structured_plan(response.content)
                    else self._default_plan(user_input, contract)
                )
                self.state.active_plan = plan_text
                self.state.history[-1].content = plan_text
                plan_shown = True
                self.ui.print_assistant(plan_text)
                tracker.advance(Phase.implement, "ดำเนินงานตามแผน")
                self.state.active_phase = Phase.implement
                if hasattr(self.ui, "update_todo"):
                    self.ui.update_todo(tracker)
                # A real gateway receives no tool schemas during planning, so a
                # text response is the expected path. Scripted/test gateways may
                # still return calls; execute them only after recording a plan.
                if not response.tool_calls:
                    continue

            if not response.tool_calls:
                if self.state.active_process_ids:
                    if finalization_repairs < self.max_finalization_repairs:
                        finalization_repairs += 1
                        process_ids = ", ".join(sorted(self.state.active_process_ids))
                        finalization_hint = (
                            f"Finalization denied by runtime (repair {finalization_repairs}/"
                            f"{self.max_finalization_repairs}): background processes are still "
                            f"active ({process_ids}). Stop every process you started before finishing."
                        )
                        continue
                    self.state.last_turn_guardrail = "final answer blocked: background processes started by this turn are still active"
                    self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                    return
                evidence = self.state.verification_evidence
                verification_current = (
                    evidence is not None
                    and evidence.workspace_revision == self.state.workspace_revision
                )
                verification_required = (
                    contract.verification_policy is VerificationPolicy.requested
                    or (
                        contract.verification_policy is VerificationPolicy.after_mutation
                        and self.state.unverified_revision is not None
                    )
                )
                if verification_required and not verification_current:
                    if finalization_repairs < self.max_finalization_repairs:
                        finalization_repairs += 1
                        hint = contract.missing_evidence_hint(verification_current)
                        finalization_hint = (
                            f"Finalization denied by runtime (repair {finalization_repairs}/"
                            f"{self.max_finalization_repairs}): {hint}. "
                            "Run one appropriate verification tool, then summarize its result."
                        )
                        tracker.advance(Phase.validate, "ต้องมีหลักฐานจาก revision ปัจจุบัน")
                        self.state.active_phase = Phase.validate
                        if hasattr(self.ui, "update_todo"):
                            self.ui.update_todo(tracker)
                        continue
                    self.state.last_turn_guardrail = (
                        "final answer blocked: requested verification has no successful evidence"
                    )
                    self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                    return
                self._trim_history()
                tracker.finish("ส่งมอบคำตอบพร้อมหลักฐาน")
                self.state.active_phase = Phase.deliver
                if hasattr(self.ui, "update_todo"):
                    self.ui.update_todo(tracker)
                if response.content:
                    final_content = self._final_with_evidence(response.content, contract)
                    if hasattr(self.ui, "print_final_answer"):
                        self.ui.print_final_answer(final_content)
                    else:
                        self.ui.print_assistant(final_content)
                elif hasattr(self.ui, "finish_steps"):
                    self.ui.finish_steps(success=True)
                return

            if response.content and not planning_phase:
                self.ui.print_assistant(response.content)

            if all(
                is_verification_attempt(call.name, call.arguments) for call in response.tool_calls
            ):
                tracker.advance(Phase.validate, "เรียกเครื่องมือตรวจสอบ")
                self.state.active_phase = Phase.validate
            else:
                tracker.advance(Phase.implement, "เรียกเครื่องมือตามแผน")
                self.state.active_phase = Phase.implement
            if hasattr(self.ui, "update_todo"):
                self.ui.update_todo(tracker)

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

            # Persist every observation before any guardrail can return. The
            # assistant tool-call message and all its tool results therefore
            # remain a protocol-valid atomic interaction even during recovery.
            for call, observation in observations:
                tool = self.tools_by_name.get(call.name)
                succeeded = "error" not in observation
                if succeeded and call.name == "start_process" and observation.get("process_id"):
                    self.state.active_process_ids.add(str(observation["process_id"]))
                if succeeded and call.name == "stop_process" and call.arguments.get("process_id"):
                    self.state.active_process_ids.discard(str(call.arguments["process_id"]))
                    if not self.state.active_process_ids:
                        finalization_hint = None
                if succeeded and tool is not None and tool.mutates_workspace:
                    self.state.workspace_revision += 1
                    self.state.unverified_revision = self.state.workspace_revision
                    self.state.tool_result_cache.clear()
                    self.state.verification_evidence = None
                if is_verification_attempt(call.name, call.arguments):
                    if is_verification_evidence(call.name, call.arguments, observation):
                        self.state.verification_evidence = VerificationEvidence(
                            tool_name=call.name,
                            workspace_revision=self.state.workspace_revision,
                            summary=_verification_summary(call.name, observation),
                        )
                        self.state.unverified_revision = None
                        finalization_hint = None
                    else:
                        # A newer failed check supersedes an older successful one.
                        self.state.verification_evidence = None
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
                    if identical_call_last_key is not None and key != identical_call_last_key:
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
                    # Results are already recorded; return to the model with the
                    # recovery hint and a protocol-complete message history.
                    break
                if consecutive_tool_failures >= self.max_consecutive_tool_failures:
                    self.state.last_turn_guardrail = (
                        "too many consecutive tool failures "
                        f"({self.max_consecutive_tool_failures}); stopped to prevent a retry loop"
                    )
                    self.ui.print_guardrail_stop(self.state.last_turn_guardrail)
                    return

        self.state.last_turn_guardrail = f"exceeded max steps ({self.max_steps})"
        self.ui.print_guardrail_stop(self.state.last_turn_guardrail)

    @staticmethod
    def _is_structured_plan(content: str | None) -> bool:
        if not content:
            return False
        lowered = content.casefold()
        has_verification = "verification" in lowered or "ตรวจสอบ" in content or "ทดสอบ" in content
        has_steps = "\n-" in content or "\n1" in content or "ขั้นตอน" in content
        return has_verification and has_steps

    @staticmethod
    def _default_plan(user_input: str, contract: TaskContract) -> str:
        steps = ["inspect the minimum relevant workspace evidence"]
        if Capability.workspace_write in contract.capabilities:
            steps.append("apply the requested change while preserving unrelated work")
        if Capability.command in contract.capabilities:
            steps.append("run the narrowest appropriate command or test")
        if Capability.process in contract.capabilities:
            steps.append("start, check, and stop any required background process")
        if Capability.web in contract.capabilities:
            steps.append("retrieve only the relevant current web information")
        numbered = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1))
        verification = (
            "Require successful evidence from the current workspace revision."
            if contract.requires_verification
            else "Check that gathered evidence directly supports the answer."
        )
        return (
            f"Plan:\nGoal: {user_input.strip()}\n{numbered}\n"
            f"Verification: {verification}\nFinish: report the outcome and concrete evidence."
        )

    def _final_with_evidence(self, content: str, contract: TaskContract) -> str:
        if not contract.requires_verification:
            return content
        evidence = self.state.verification_evidence
        if evidence is None or evidence.workspace_revision != self.state.workspace_revision:
            return content
        if evidence.summary.casefold() in content.casefold():
            return content
        return f"{content.rstrip()}\n\nVerification: {evidence.summary}"

    def _verification_is_impossible(
        self, contract: TaskContract, active_specs: list[ToolSpec]
    ) -> bool:
        if not contract.requires_verification:
            return False
        active_names = {spec.name for spec in active_specs}
        verifiers = {
            name
            for name in active_names
            if name in {"run_tests", "run_cli", "wait_for_http", "check_url_health"}
            or bool(self.tools_by_name.get(name) and self.tools_by_name[name].evidence_type)
        }
        if contract.verification_policy is VerificationPolicy.requested:
            return not verifiers
        mutations_available = any(
            self.tools_by_name[name].mutates_workspace
            for name in active_names
            if name in self.tools_by_name
        )
        return mutations_available and not verifiers

    _COMMON_AGENTIC_TOOLS = {"ask_user_choice", "load_skill"}
    _TOOL_SETS_BY_CAPABILITY: dict[Capability, set[str]] = {
        Capability.workspace_read: {
            "read_file",
            "list_files",
            "grep",
            "git_status",
            "git_diff",
            "git_log",
            "read_directory_tree",
            "summarize_text",
            "compare_files",
            "memory_read",
            "todo_list",
        },
        Capability.workspace_write: {
            "write_file",
            "edit_file",
            "apply_diff",
            "memory_write",
            "todo_create",
            "todo_update",
        },
        Capability.command: {
            "run_tests",
            "execute_python",
            "run_cli",
        },
        Capability.process: {
            "start_process",
            "read_process_output",
            "stop_process",
            "wait_for_http",
            "list_processes",
            "kill_process",
            "check_url_health",
        },
        Capability.web: {
            "fetch_url",
            "search_web",
        },
    }

    def _active_tool_specs(self, contract: TaskContract) -> list[ToolSpec]:
        """Return the union of tools required by independent task capabilities."""
        if contract.response_mode is ResponseMode.direct:
            return []
        allowed = set(self._COMMON_AGENTIC_TOOLS)
        for capability in contract.capabilities:
            allowed.update(self._TOOL_SETS_BY_CAPABILITY.get(capability, set()))
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

        # Second: message-count cap. Tool request/result groups are atomic.
        if len(self.state.history) > self.max_history_messages:
            self.state.history = trim_messages_to_count(
                self.state.history,
                self.max_history_messages,
                preserve_system=True,
                preserve_latest_user=True,
            )

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
        self.state.active_contract = None
        self.state.active_plan = None
        self.state.verification_evidence = None
        self.state.unverified_revision = None
        self.state.tool_result_cache.clear()
        if clear_all:
            self.state.always_allowed.clear()
        self._update_ui_status()

    def status(self, workspace: str) -> dict:
        return {
            "session_started_at": self.started_at,
            "model": self.model or "default",
            "mode": self.mode.value,
            "workspace": workspace,
            "phase": self.state.active_phase.value,
            "workspace_revision": self.state.workspace_revision,
            "unverified_revision": self.state.unverified_revision,
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
            if tool is not None and tool.idempotent and not tool.volatile:
                cache_key = (call.name, self._cache_key_for_arguments(call.arguments))
                cached = self.state.tool_result_cache.get(cache_key)
                if cached is not None:
                    self.ui.print_tool_call(call.name, call.arguments)
                    if hasattr(self.ui, "print_tool_result"):
                        self.ui.print_tool_result(call.name, cached)
                    return call.id, cached
            observation = await self._invoke_tool_handler(call)
            if cache_key is not None and "error" not in observation:
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
