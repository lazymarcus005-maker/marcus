from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import RiskTier, ToolExecutionStatus
from harness.db.models import AgentRun, ToolExecution
from harness.llm.types import ToolCall
from harness.runtime.tools import ExecutionOutcome, Tool


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
        idempotency_key = f"{run.id}:{step_no}:{call_index}"
        existing = await self._get_existing(idempotency_key)

        if tool is None:
            return await self._record_unknown_tool(run, step_no, call_index, call, existing)

        if existing is not None:
            return await self._resolve_existing(existing, tool)

        execution = ToolExecution(
            run_id=run.id,
            step_no=step_no,
            call_index=call_index,
            idempotency_key=idempotency_key,
            tool_name=tool.name,
            risk_tier=tool.risk_tier,
            idempotent=tool.idempotent,
            args=call.arguments,
            status=ToolExecutionStatus.started,
        )
        self.session.add(execution)
        await self.session.commit()  # write-ahead: durable before the handler runs

        return await self._invoke_and_finalize(execution, tool)

    async def _record_unknown_tool(
        self,
        run: AgentRun,
        step_no: int,
        call_index: int,
        call: ToolCall,
        existing: ToolExecution | None,
    ) -> ExecutionOutcome:
        if existing is not None:
            return ExecutionOutcome(observation={"error": existing.error or "unknown tool"})

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
        return ExecutionOutcome(observation={"error": error})

    async def _resolve_existing(self, existing: ToolExecution, tool: Tool) -> ExecutionOutcome:
        if existing.status == ToolExecutionStatus.succeeded:
            return ExecutionOutcome(observation=existing.result or {})
        if existing.status in (ToolExecutionStatus.failed, ToolExecutionStatus.unknown):
            return ExecutionOutcome(observation={"error": existing.error or "tool call failed"})
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
        fatal = execution.risk_tier in (RiskTier.sensitive_write, RiskTier.destructive)
        execution.error = (
            "crashed mid-execution; outcome unknown and this tool is not safe to "
            "auto-retry"
            + (" (requires manual review — approval workflow is issue #17)" if fatal else "")
        )
        await self.session.commit()
        return ExecutionOutcome(
            observation={"error": execution.error},
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
            return ExecutionOutcome(observation={"error": str(exc)})

        execution.status = ToolExecutionStatus.succeeded
        execution.result = result if isinstance(result, dict) else {"value": result}
        execution.finished_at = datetime.now(UTC)
        await self.session.commit()
        return ExecutionOutcome(observation=execution.result)

    async def _get_existing(self, idempotency_key: str) -> ToolExecution | None:
        result = await self.session.execute(
            sa.select(ToolExecution).where(ToolExecution.idempotency_key == idempotency_key)
        )
        return result.scalar_one_or_none()
