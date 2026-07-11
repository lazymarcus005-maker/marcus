import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import MessageRole, RunStatus, StepType
from harness.db.models import AgentRun, AgentStep
from harness.llm.gateway import LLMError, LLMGateway
from harness.llm.types import ToolCall
from harness.llm.usage import record_usage
from harness.runtime import guardrails
from harness.runtime.compaction import maybe_compact
from harness.runtime.context import build_llm_messages
from harness.runtime.native_tools import (
    ASK_USER_TOOL_NAME,
    FINISH_TOOL_NAME,
    build_ask_user_tool,
    build_finish_tool,
)
from harness.runtime.repository import RunRepository
from harness.runtime.tool_executor import ToolExecutor
from harness.runtime.tools import Tool

# Control results returned by _process_tool_calls, consumed by _execute_step.
ControlResult = tuple[str, dict[str, Any]] | None


class RunEngine:
    """The ReAct loop: load state -> LLM -> validate -> execute tools -> persist -> loop.

    One run_until_blocked() call advances a run step by step until it hits a
    terminal state (completed/failed/cancelled/timed_out), a waiting state
    (waiting_user_input/waiting_approval), or a guardrail stops it. It never
    raises for run-level failures — those become a checkpointed status with
    run.error set; only truly unexpected errors (e.g. the run not existing)
    raise.
    """

    def __init__(
        self, session: AsyncSession, llm: LLMGateway, tools: list[Tool] | None = None
    ) -> None:
        self.session = session
        self.repo = RunRepository(session)
        self.llm = llm
        self.tool_executor = ToolExecutor(session)
        self.tools_by_name = {t.name: t for t in (tools or [])}
        self._finish_tool = build_finish_tool()
        self._ask_user_tool = build_ask_user_tool()

    async def run_until_blocked(self, run_id: uuid.UUID) -> AgentRun:
        run = await self.repo.get_with_history(run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")

        if run.status == RunStatus.pending:
            run = await self.repo.checkpoint(run, status=RunStatus.running)
            await self.session.commit()

        while run.status == RunStatus.running:
            run = await self._execute_step(run)

        return run

    async def _execute_step(self, run: AgentRun) -> AgentRun:
        try:
            guardrails.check_before_step(run)
        except guardrails.GuardrailViolation as violation:
            run = await self.repo.checkpoint(run, status=violation.status, error=violation.reason)
            await self.session.commit()
            return run

        step_no = run.current_step
        existing_step = await self._get_step(run.id, step_no)

        if existing_step is None:
            outcome = await self._call_llm(run, step_no)
            if outcome is None:
                # LLM call failed; the terminal checkpoint was already committed.
                run = await self.repo.get(run.id) or run
                return run
            tool_calls_payload, plain_content, tokens_used = outcome
        else:
            tool_calls_payload = existing_step.payload.get("tool_calls", [])
            plain_content = existing_step.payload.get("content")
            tokens_used = run.tokens_used

        if guardrails.token_budget_exceeded(run, tokens_used):
            run = await self.repo.checkpoint(
                run,
                status=RunStatus.timed_out,
                error="token budget exceeded",
                tokens_used=tokens_used,
                current_step=step_no + 1,
            )
            await self.session.commit()
            return run

        if not tool_calls_payload:
            if plain_content:
                await self.repo.add_message(run.id, MessageRole.assistant, plain_content)
            run = await self.repo.checkpoint(
                run,
                status=RunStatus.waiting_user_input,
                current_step=step_no + 1,
                tokens_used=tokens_used,
            )
            await self.session.commit()
            return run

        try:
            guardrails.check_tool_call_budget(run, len(tool_calls_payload))
        except guardrails.GuardrailViolation as violation:
            run = await self.repo.checkpoint(
                run,
                status=violation.status,
                error=violation.reason,
                current_step=step_no + 1,
                tokens_used=tokens_used,
            )
            await self.session.commit()
            return run

        control_result = await self._process_tool_calls(run, step_no, tool_calls_payload)

        field_updates: dict[str, Any] = {
            "current_step": step_no + 1,
            "tokens_used": tokens_used,
            "tool_calls_used": run.tool_calls_used + len(tool_calls_payload),
        }

        if control_result is None:
            run = await self.repo.checkpoint(run, **field_updates)
            await self.session.commit()
            return run

        kind, args = control_result
        if kind == "completed":
            run = await self.repo.checkpoint(
                run, status=RunStatus.completed, final_result=args, **field_updates
            )
        elif kind == "waiting_user_input":
            await self.repo.add_message(
                run.id, MessageRole.assistant, args.get("question", "(no question provided)")
            )
            run = await self.repo.checkpoint(
                run, status=RunStatus.waiting_user_input, **field_updates
            )
        elif kind == "failed":
            run = await self.repo.checkpoint(
                run,
                status=RunStatus.failed,
                error=args.get("error", "unknown error"),
                **field_updates,
            )
        else:
            raise AssertionError(f"unhandled control result kind: {kind}")

        await self.session.commit()
        return run

    async def _call_llm(
        self, run: AgentRun, step_no: int
    ) -> tuple[list[dict], str | None, int] | None:
        """Call the LLM and persist the resulting step. Returns None if the call failed

        (in which case the run has already been checkpointed to Failed and committed).
        """
        await maybe_compact(self.session, self.llm, run)
        messages = await build_llm_messages(self.session, run)
        tool_specs = [
            t.to_spec()
            for t in (self._finish_tool, self._ask_user_tool, *self.tools_by_name.values())
        ]

        try:
            response = await self.llm.complete(messages, tools=tool_specs)
        except LLMError as exc:
            await self.repo.checkpoint(run, status=RunStatus.failed, error=str(exc))
            await self.session.commit()
            return None

        tool_calls_payload = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls
        ]
        payload = {
            "content": response.content,
            "tool_calls": tool_calls_payload,
            "finish_reason": response.finish_reason,
            "model": response.model,
        }
        await self.repo.add_step(
            run.id,
            step_no,
            StepType.llm_call,
            payload,
            token_usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        )
        await record_usage(
            self.session,
            tenant_id=run.tenant_id,
            run_id=run.id,
            model=response.model,
            usage=response.usage,
        )

        tokens_used = run.tokens_used + response.usage.total_tokens
        return tool_calls_payload, response.content, tokens_used

    async def _process_tool_calls(
        self, run: AgentRun, step_no: int, tool_calls_payload: list[dict]
    ) -> ControlResult:
        for call_index, raw_call in enumerate(tool_calls_payload):
            call = ToolCall(
                id=raw_call["id"], name=raw_call["name"], arguments=raw_call["arguments"]
            )

            if call.name == FINISH_TOOL_NAME:
                outcome = await self.tool_executor.execute(
                    run, step_no, call_index, call, tool=self._finish_tool
                )
                if "error" not in outcome.observation:
                    return "completed", outcome.observation
                continue

            if call.name == ASK_USER_TOOL_NAME:
                outcome = await self.tool_executor.execute(
                    run, step_no, call_index, call, tool=self._ask_user_tool
                )
                if "error" not in outcome.observation:
                    return "waiting_user_input", outcome.observation
                continue

            tool = self.tools_by_name.get(call.name)

            if tool is not None:
                try:
                    await guardrails.check_repeated_calls(
                        self.session, run.id, tool.name, call.arguments
                    )
                except guardrails.GuardrailViolation as violation:
                    return "failed", {"error": violation.reason}

            outcome = await self.tool_executor.execute(run, step_no, call_index, call, tool=tool)
            if outcome.fatal:
                return "failed", {"error": outcome.fatal_reason}

        return None

    async def _get_step(self, run_id: uuid.UUID, step_no: int) -> AgentStep | None:
        result = await self.session.execute(
            sa.select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.step_no == step_no)
        )
        return result.scalar_one_or_none()
