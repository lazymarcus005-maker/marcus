from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.config import get_settings
from harness.db.enums import ApprovalStatus, RiskTier, ToolExecutionStatus
from harness.db.models import AgentRun, ApprovalRequest, ToolExecution
from harness.llm.types import ToolCall
from harness.observability import TOOL_CALLS, span
from harness.runtime import guardrails
from harness.runtime.result_pipeline import truncate_result
from harness.runtime.tool_validation import ToolArgumentError, normalize_and_validate_arguments
from harness.runtime.tools import (
    ExecutionOutcome,
    Tool,
    ToolErrorCode,
    ToolRuntimeError,
    error_observation,
    success_observation,
)


class ToolExecutor:
    """Executes tool calls with write-ahead persistence and crash-recovery (decisions.md D6).

    The idempotency key (run_id:step_no:call_index) is written to tool_executions
    with status=started *before* the handler runs, so a crash mid-call is
    detectable on resume: looking the key up again finds the same row instead
    of a fresh insert, and recovery policy is applied based on risk_tier.

    Every state change that matters for crash detection is committed, not just
    flushed — the whole point of write-ahead is that it survives a crash that
    happens before the *next* commit the caller would have made.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(
        self, run: AgentRun, step_no: int, call_index: int, call: ToolCall, tool: Tool | None
    ) -> ExecutionOutcome:
        with span(
            "agent.tool_call",
            run_id=str(run.id),
            tenant_id=str(run.tenant_id),
            step_no=step_no,
            tool_name=call.name,
        ):
            outcome = await self._execute_inner(run, step_no, call_index, call, tool)
            TOOL_CALLS.labels(
                tool=call.name,
                status=(
                    "approval"
                    if outcome.needs_approval
                    else "fatal"
                    if outcome.fatal
                    else "error"
                    if "error" in outcome.observation
                    else "ok"
                ),
            ).inc()
            return outcome

    async def _execute_inner(
        self, run: AgentRun, step_no: int, call_index: int, call: ToolCall, tool: Tool | None
    ) -> ExecutionOutcome:
        idempotency_key = f"{run.id}:{step_no}:{call_index}"
        existing = await self._get_existing(idempotency_key)

        if tool is None:
            return await self._record_unknown_tool(run, step_no, call_index, call, existing)

        if existing is not None:
            return await self._resolve_existing(existing, tool)

        try:
            call.arguments = normalize_and_validate_arguments(tool.parameters, call.arguments)
        except ToolArgumentError as exc:
            return await self._record_invalid_arguments(run, step_no, call_index, call, tool, exc)

        if guardrails.requires_approval(tool.risk_tier):
            gated_outcome = await self._check_approval(run, step_no, call_index, call, tool)
            if gated_outcome is not None:
                return gated_outcome
            # else: approved — fall through to the normal write-ahead execution below

        execution = ToolExecution(
            run_id=run.id,
            step_no=step_no,
            call_index=call_index,
            idempotency_key=idempotency_key,
            tool_name=tool.name,
            mcp_server_id=tool.mcp_server_id,
            risk_tier=tool.risk_tier,
            idempotent=tool.idempotent,
            args=call.arguments,
            status=ToolExecutionStatus.started,
        )
        self.session.add(execution)
        await self.session.commit()  # write-ahead: durable before the handler runs

        return await self._invoke_and_finalize(execution, tool)

    async def _record_invalid_arguments(
        self,
        run: AgentRun,
        step_no: int,
        call_index: int,
        call: ToolCall,
        tool: Tool,
        error: ToolArgumentError,
    ) -> ExecutionOutcome:
        execution = ToolExecution(
            run_id=run.id,
            step_no=step_no,
            call_index=call_index,
            idempotency_key=f"{run.id}:{step_no}:{call_index}",
            tool_name=tool.name,
            mcp_server_id=tool.mcp_server_id,
            risk_tier=tool.risk_tier,
            idempotent=tool.idempotent,
            args=call.arguments,
            status=ToolExecutionStatus.failed,
            error=str(error),
            finished_at=datetime.now(UTC),
        )
        self.session.add(execution)
        await self.session.commit()
        return ExecutionOutcome(
            observation=error_observation(
                str(error),
                code=error.code,
                retryable=True,
                evidence_id=execution.id,
            )
        )

    async def _check_approval(
        self, run: AgentRun, step_no: int, call_index: int, call: ToolCall, tool: Tool
    ) -> ExecutionOutcome | None:
        """Approval gate for sensitive_write/destructive tools (issue #17).

        Runs *before* any write-ahead ToolExecution row exists, so a
        pending/rejected call never looks like a started-then-crashed
        execution. Returns None when approved (caller proceeds to the normal
        write-ahead path); otherwise returns the outcome to short-circuit on.
        """
        result = await self.session.execute(
            sa.select(ApprovalRequest).where(
                ApprovalRequest.run_id == run.id,
                ApprovalRequest.step_no == step_no,
                ApprovalRequest.call_index == call_index,
            )
        )
        approval = result.scalar_one_or_none()

        if approval is None:
            settings = get_settings()
            approval = ApprovalRequest(
                tenant_id=run.tenant_id,
                run_id=run.id,
                step_no=step_no,
                call_index=call_index,
                tool_name=tool.name,
                risk_tier=tool.risk_tier,
                args=call.arguments,
                status=ApprovalStatus.pending,
                expires_at=datetime.now(UTC) + timedelta(hours=settings.approval_expiry_hours),
            )
            self.session.add(approval)
            await self.session.commit()

        if approval.status == ApprovalStatus.pending:
            return ExecutionOutcome(observation={}, needs_approval=True)

        if approval.status == ApprovalStatus.approved:
            return None

        # rejected or expired: terminal — write the failed execution now so
        # replay short-circuits via _resolve_existing without touching the
        # approval again.
        error = f"tool call was {approval.status.value} by approver" + (
            f": {approval.reason}" if approval.reason else ""
        )
        execution = ToolExecution(
            run_id=run.id,
            step_no=step_no,
            call_index=call_index,
            idempotency_key=f"{run.id}:{step_no}:{call_index}",
            tool_name=tool.name,
            mcp_server_id=tool.mcp_server_id,
            risk_tier=tool.risk_tier,
            idempotent=tool.idempotent,
            args=call.arguments,
            status=ToolExecutionStatus.failed,
            error=error,
            finished_at=datetime.now(UTC),
        )
        self.session.add(execution)
        await self.session.commit()
        return ExecutionOutcome(
            observation=error_observation(
                error,
                code=ToolErrorCode.approval_denied,
                retryable=False,
                evidence_id=execution.id,
            )
        )

    async def _record_unknown_tool(
        self,
        run: AgentRun,
        step_no: int,
        call_index: int,
        call: ToolCall,
        existing: ToolExecution | None,
    ) -> ExecutionOutcome:
        if existing is not None:
            return ExecutionOutcome(
                observation=error_observation(
                    existing.error or "unknown tool",
                    code=ToolErrorCode.unknown_tool,
                    retryable=False,
                    evidence_id=existing.id,
                )
            )

        error = f"unknown tool: {call.name}"
        execution = ToolExecution(
            run_id=run.id,
            step_no=step_no,
            call_index=call_index,
            idempotency_key=f"{run.id}:{step_no}:{call_index}",
            tool_name=call.name,
            risk_tier=RiskTier.read_only,
            idempotent=False,
            args=call.arguments,
            status=ToolExecutionStatus.failed,
            error=error,
            finished_at=datetime.now(UTC),
        )
        self.session.add(execution)
        await self.session.commit()
        return ExecutionOutcome(
            observation=error_observation(
                error,
                code=ToolErrorCode.unknown_tool,
                retryable=False,
                evidence_id=execution.id,
            )
        )

    async def _resolve_existing(self, existing: ToolExecution, tool: Tool) -> ExecutionOutcome:
        if existing.status == ToolExecutionStatus.succeeded:
            return ExecutionOutcome(
                observation=success_observation(existing.result or {}, evidence_id=existing.id)
            )
        if existing.status in (ToolExecutionStatus.failed, ToolExecutionStatus.unknown):
            code = (
                ToolErrorCode.outcome_unknown
                if existing.status == ToolExecutionStatus.unknown
                else ToolErrorCode.execution_failed
            )
            return ExecutionOutcome(
                observation=error_observation(
                    existing.error or "tool call failed",
                    code=code,
                    retryable=False,
                    evidence_id=existing.id,
                )
            )
        # status == started: we crashed after the write-ahead insert but before finishing.
        return await self._recover_started(existing, tool)

    async def _recover_started(self, execution: ToolExecution, tool: Tool) -> ExecutionOutcome:
        safe_to_retry = execution.risk_tier == RiskTier.read_only or (
            execution.risk_tier == RiskTier.low_risk_write and execution.idempotent
        )
        if safe_to_retry:
            return await self._invoke_and_finalize(execution, tool)

        execution.status = ToolExecutionStatus.unknown
        execution.finished_at = datetime.now(UTC)
        # This is crash forensics, not the pre-execution approval gate (that
        # only ever runs before a ToolExecution row exists — see
        # _check_approval). Here the handler may already have run against the
        # real world; the outcome is unrecoverable and needs a human to look,
        # not another approval prompt.
        fatal = execution.risk_tier in (RiskTier.sensitive_write, RiskTier.destructive)
        execution.error = (
            "crashed mid-execution; outcome unknown and this tool is not safe to "
            "auto-retry" + (" (requires manual review)" if fatal else "")
        )
        await self.session.commit()
        return ExecutionOutcome(
            observation=error_observation(
                execution.error,
                code=ToolErrorCode.outcome_unknown,
                retryable=False,
                evidence_id=execution.id,
            ),
            fatal=fatal,
            fatal_reason=execution.error if fatal else None,
        )

    async def _invoke_and_finalize(self, execution: ToolExecution, tool: Tool) -> ExecutionOutcome:
        try:
            result = await tool.handler(execution.args)
        except Exception as exc:  # noqa: BLE001 - tool failures become observations, not crashes
            execution.status = ToolExecutionStatus.failed
            execution.error = str(exc)
            execution.finished_at = datetime.now(UTC)
            await self.session.commit()
            code = exc.code if isinstance(exc, ToolRuntimeError) else ToolErrorCode.execution_failed
            retryable = (
                exc.retryable
                if isinstance(exc, ToolRuntimeError)
                else tool.risk_tier == RiskTier.read_only or tool.idempotent
            )
            return ExecutionOutcome(
                observation=error_observation(
                    str(exc),
                    code=code,
                    retryable=retryable,
                    evidence_id=execution.id,
                )
            )

        execution.status = ToolExecutionStatus.succeeded
        execution.result = truncate_result(result, max_chars=get_settings().tool_result_max_chars)
        execution.finished_at = datetime.now(UTC)
        await self.session.commit()
        return ExecutionOutcome(
            observation=success_observation(execution.result, evidence_id=execution.id)
        )

    async def _get_existing(self, idempotency_key: str) -> ToolExecution | None:
        result = await self.session.execute(
            sa.select(ToolExecution).where(ToolExecution.idempotency_key == idempotency_key)
        )
        return result.scalar_one_or_none()
